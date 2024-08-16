from transformers import AutoConfig, AutoModel, AutoModelForCausalLM
from .configuration_smile_mistral import SmileMistralConfig
from .modeling_smile_mistral import SmileMistralModel, SmileMistralForCausalLM

AutoConfig.register("smile_mistral", SmileMistralConfig)
AutoModel.register(SmileMistralConfig, SmileMistralModel)
AutoModelForCausalLM.register(SmileMistralConfig, SmileMistralForCausalLM)
