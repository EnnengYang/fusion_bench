import functools
import logging
import os

import torch
from omegaconf import DictConfig
from torch import Tensor
from torch.utils.data import DataLoader
from transformers import CLIPModel, CLIPProcessor

from fusion_bench.dataset import CLIPDataset
from fusion_bench.models.hf_clip import HFCLIPClassifier
from fusion_bench.tasks.clip_classification import get_classnames_and_templates
from fusion_bench.utils import timeit_context
from fusion_bench.modelpool import CLIPVisionModelPool

from .task_wise_adamerging import TaskWiseAdaMergingAlgorithm

log = logging.getLogger(__name__)


class InfiniteDataLoader:
    def __init__(self, data_loader):
        self.data_loader = data_loader
        self.data_iter = iter(data_loader)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            data = next(self.data_iter)
        except StopIteration:
            self.data_iter = iter(self.data_loader)  # Reset the data loader
            data = next(self.data_iter)
        return data


class CLIPTaskWiseAdaMergingAlgorithm(TaskWiseAdaMergingAlgorithm):
    modelpool: CLIPVisionModelPool = None
    _clip_processor: CLIPProcessor = None
    zeroshot_weights = {}

    def __init__(self, algorithm_config: DictConfig):
        super().__init__(algorithm_config)

    @functools.cache
    def get_test_dataset(self, task: str):
        """
        Load the test dataset for the task.
        This method is cached, so the dataset is loaded only once.
        """
        log.info(f"Loading test dataset: {task}")
        dataset = self.modelpool.load_test_dataset(task)
        dataset = CLIPDataset(dataset, self._clip_processor)
        return dataset

    @functools.cache
    def get_shuffled_test_loader_iter(self, task: str):
        loader = DataLoader(
            self.get_test_dataset(task),
            batch_size=self.config.batch_size,
            shuffle=True,
            num_workers=self.config.num_workers,
            pin_memory=True,
        )
        if self._fabric is not None:
            loader = self._fabric.setup_dataloaders(loader)
        return iter(InfiniteDataLoader(loader))

    def on_test_time_adaptation_start(self):
        """
        Here we load the CLIP processor and construct the zero-shot classification head for each task.
        """
        clip_model_config = self.modelpool.get_model_config("_pretrained_")
        pretrained_path = (
            clip_model_config.pretrained_model_name_or_path
            if hasattr(clip_model_config, "pretrained_model_name_or_path")
            else clip_model_config.path
        )

        with timeit_context("Loading CLIP processor and pretrained CLIP model."):
            self._clip_processor = CLIPProcessor.from_pretrained(pretrained_path)
            clip_model: CLIPModel = CLIPModel.from_pretrained(pretrained_path)

            clip_classifier = HFCLIPClassifier(clip_model, self._clip_processor)
            self.visual_projection = clip_model.visual_projection.requires_grad_(False)
            self.logit_scale_exp = clip_model.logit_scale.exp()
            if self._fabric is not None:
                self.visual_projection = self._fabric.to_device(self.visual_projection)
                self.logit_scale_exp = self._fabric.to_device(self.logit_scale_exp)

        for task in self.modelpool.model_names:
            cache_file = os.path.join(
                self.config.cache_dir,
                f"{os.path.basename(pretrained_path)}_{task}_zeroshot_weights.pt",
            )
            if os.path.exists(cache_file):
                log.info(f"Loading cached zeroshot weights for task: {task}")
                zeroshot_weights = torch.load(cache_file, map_location="cpu")
            else:
                log.info(f"Construct zero shot classification head for task: {task}")
                classnames, templates = get_classnames_and_templates(task)
                clip_classifier.set_classification_task(classnames, templates)
                zeroshot_weights = clip_classifier.zeroshot_weights
                log.info(f"save zeroshot weights to {cache_file}")
                torch.save(zeroshot_weights, cache_file)
            self.zeroshot_weights[task] = zeroshot_weights
            if self._fabric is not None:
                self.zeroshot_weights[task] = self._fabric.to_device(
                    self.zeroshot_weights[task]
                )

    def compute_logits(self, module, batch, task: str) -> Tensor:
        images, _ = batch
        text_embeds = self.zeroshot_weights[task]

        image_embeds = module(images)[1]
        image_embeds = self.visual_projection(image_embeds)

        # normalize embeddings
        image_embeds = image_embeds / image_embeds.norm(p=2, dim=-1, keepdim=True)

        # cosine similarity
        logits_per_text = (
            torch.matmul(text_embeds, image_embeds.t()) * self.logit_scale_exp
        )
        logits_per_image = logits_per_text.t()

        return logits_per_image