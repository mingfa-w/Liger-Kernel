import inspect
import logging

from functools import partial
from types import MethodType
from typing import Callable

import transformers

from packaging import version
from transformers import PreTrainedModel

from liger_kernel.transformers.cross_entropy import LigerCrossEntropyLoss
from liger_kernel.transformers.functional import liger_cross_entropy
from liger_kernel.transformers.geglu import LigerGEGLUMLP
from liger_kernel.transformers.layer_norm import LigerLayerNorm
from liger_kernel.transformers.model.gemma import lce_forward as gemma_lce_forward
from liger_kernel.transformers.model.gemma import lce_forward_deprecated as gemma_lce_forward_deprecated
from liger_kernel.transformers.model.gemma2 import lce_forward as gemma2_lce_forward
from liger_kernel.transformers.model.gemma2 import lce_forward_deprecated as gemma2_lce_forward_deprected
from liger_kernel.transformers.model.llama import lce_forward as llama_lce_forward
from liger_kernel.transformers.model.llama import lce_forward_deprecated as llama_lce_forward_deprecated
from liger_kernel.transformers.model.llava import lce_forward as llava_lce_forward
from liger_kernel.transformers.model.llava import lce_forward_deprecated as llava_lce_forward_deprecated
from liger_kernel.transformers.model.mistral import lce_forward as mistral_lce_forward
from liger_kernel.transformers.model.mixtral import lce_forward as mixtral_lce_forward
from liger_kernel.transformers.model.mixtral import lce_forward_deprecated as mixtral_lce_forward_deprecated
from liger_kernel.transformers.model.phi3 import lce_forward as phi3_lce_forward
from liger_kernel.transformers.model.phi3 import lce_forward_deprecated as phi3_lce_forward_deprecated
from liger_kernel.transformers.model.qwen2 import lce_forward as qwen2_lce_forward
from liger_kernel.transformers.model.qwen2 import lce_forward_deprecated as qwen2_lce_forward_deprecated
from liger_kernel.transformers.model.smollm3 import lce_forward as smollm3_lce_forward
from liger_kernel.transformers.qwen2vl_mrope import liger_multimodal_rotary_pos_emb
from liger_kernel.transformers.rms_norm import LigerRMSNorm
from liger_kernel.transformers.rope import liger_rotary_pos_emb
from liger_kernel.transformers.swiglu import LigerBlockSparseTop2MLP
from liger_kernel.transformers.swiglu import LigerPhi3SwiGLUMLP
from liger_kernel.transformers.swiglu import LigerSwiGLUMLP

try:
    import peft

    PEFT_AVAILABLE = True
except ImportError:
    PEFT_AVAILABLE = False

transformer_version = version.parse(transformers.__version__)

logger = logging.getLogger(__name__)
SUPPORTED_TRANSFORMER_VERSION = "4.46.1"
TRANSFORMER_DEPRECATION_WARNING = "Support for transformers versions < 4.46.1 will soon be discontinued due to issues with incorrect gradient accumulation. \n Please consider upgrading to avoid potential issues. See details: https://github.com/huggingface/transformers/pull/34191"


def _bind_method_to_module(module, method_name: str, new_method: Callable):
    # Binds a new method to a module instance so that self is passed as the first argument
    module.__dict__[method_name] = new_method.__get__(module, module.__class__)


def _patch_rms_norm_module(module, offset=0.0, eps=1e-6, casting_mode="llama", in_place=True, row_mode=None):
    # Check if the module is a PEFT ModulesToSaveWrapper
    # If it is, we need to patch the modules_to_save.default and original_modules
    if PEFT_AVAILABLE and isinstance(module, peft.utils.other.ModulesToSaveWrapper):
        module.modules_to_save.default.offset = offset
        module.modules_to_save.default.casting_mode = casting_mode
        module.modules_to_save.default.variance_epsilon = (
            getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        )
        module.modules_to_save.default.in_place = in_place
        module.modules_to_save.default.row_mode = row_mode
        module.original_module.offset = offset
        module.original_module.casting_mode = casting_mode
        module.original_module.variance_epsilon = (
            getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        )
        module.original_module.in_place = in_place
        module.original_module.row_mode = row_mode
        _bind_method_to_module(module.modules_to_save.default, "forward", LigerRMSNorm.forward)
        _bind_method_to_module(module.modules_to_save.default, "extra_repr", LigerRMSNorm.extra_repr)
        _bind_method_to_module(module.original_module, "forward", LigerRMSNorm.forward)
        _bind_method_to_module(module.original_module, "extra_repr", LigerRMSNorm.extra_repr)
        _bind_method_to_module(module.modules_to_save.default, "_get_name", lambda self: LigerRMSNorm.__name__)
        _bind_method_to_module(module.original_module, "_get_name", lambda self: LigerRMSNorm.__name__)
    else:
        module.offset = offset
        module.casting_mode = casting_mode
        module.variance_epsilon = getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        module.in_place = in_place
        module.row_mode = row_mode
        _bind_method_to_module(module, "forward", LigerRMSNorm.forward)
        _bind_method_to_module(module, "extra_repr", LigerRMSNorm.extra_repr)
        _bind_method_to_module(module, "_get_name", lambda self: LigerRMSNorm.__name__)


def _patch_layer_norm_module(module, eps=1e-6):
    # Check if the module is a PEFT ModulesToSaveWrapper
    # If it is, we need to patch the modules_to_save.default and original_modules
    if PEFT_AVAILABLE and isinstance(module, peft.utils.other.ModulesToSaveWrapper):
        module.hidden_size = module.normalized_shape
        _bind_method_to_module(module, "forward", LigerLayerNorm.forward)
        _bind_method_to_module(module, "extra_repr", LigerLayerNorm.extra_repr)
        module.modules_to_save.default.variance_epsilon = (
            getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        )
        module.original_module.hidden_size = getattr(module, "hidden_size", None) or getattr(
            module, "normalized_shape", None
        )
        module.original_module.variance_epsilon = (
            getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        )
        module.original_module.hidden_size = getattr(module, "hidden_size", None) or getattr(
            module, "normalized_shape", None
        )
        _bind_method_to_module(module.modules_to_save.default, "forward", LigerLayerNorm.forward)
        _bind_method_to_module(module.modules_to_save.default, "extra_repr", LigerLayerNorm.extra_repr)
        _bind_method_to_module(module.original_module, "forward", LigerLayerNorm.forward)
        _bind_method_to_module(module.original_module, "extra_repr", LigerLayerNorm.extra_repr)
        _bind_method_to_module(module.modules_to_save.default, "_get_name", lambda self: LigerLayerNorm.__name__)
        _bind_method_to_module(module.original_module, "_get_name", lambda self: LigerLayerNorm.__name__)
    else:
        module.variance_epsilon = getattr(module, "variance_epsilon", None) or getattr(module, "eps", None) or eps
        module.hidden_size = getattr(module, "hidden_size", None) or getattr(module, "normalized_shape", None)
        _bind_method_to_module(module, "forward", LigerLayerNorm.forward)
        _bind_method_to_module(module, "extra_repr", LigerLayerNorm.extra_repr)
        _bind_method_to_module(module, "_get_name", lambda self: LigerLayerNorm.__name__)


def _patch_swiglu_module(module, liger_module):
    _bind_method_to_module(module, "forward", liger_module.forward)
    _bind_method_to_module(module, "_get_name", lambda self: liger_module.__name__)


def _patch_geglu_module(module):
    _bind_method_to_module(module, "forward", LigerGEGLUMLP.forward)
    _bind_method_to_module(module, "_get_name", lambda self: LigerGEGLUMLP.__name__)


def apply_liger_kernel_to_granite(
    rope: bool = True,
    cross_entropy: bool = True,
    fused_linear_cross_entropy: bool = False,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Granite 3 models

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is True.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is False.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.



    Debugging notes:
        If LigerSwiGLUMLP is OK for Llama, it should be fine for Granite, but it's not.
    """

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.granite import modeling_granite
    from transformers.models.granite.modeling_granite import GraniteModel

    if swiglu:
        modeling_granite.GraniteMLP = LigerSwiGLUMLP

    if rms_norm:
        modeling_granite.GraniteRMSNorm = LigerRMSNorm

    if rope:
        modeling_granite.apply_rotary_pos_emb = liger_rotary_pos_emb

    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_granite.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        raise NotImplementedError("LigerFusedLinearCrossEntropy is not available for Granite models.")
        # NOTE: Granite model `GraniteForCausalLM.forward` scales logits each
        # call, so we can't sidestep logit materialization. A bit more work
        # would be needed to add a scaling term to the `LigerFusedLinearCrossEntropyFunction`
        # for the logit output.

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules (e.g. GraniteRMSNorm or GraniteMLP)

        # get the base model from the model instance
        base_model: GraniteModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_llama(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Llama models (2 and 3)

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.llama import modeling_llama
    from transformers.models.llama.modeling_llama import LlamaModel

    if rope:
        modeling_llama.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_llama.LlamaRMSNorm = LigerRMSNorm
    if swiglu:
        modeling_llama.LlamaMLP = LigerSwiGLUMLP

    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_llama.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(llama_lce_forward, model)
            else:
                modeling_llama.LlamaForCausalLM.forward = llama_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(llama_lce_forward_deprecated, model)
            else:
                modeling_llama.LlamaForCausalLM.forward = llama_lce_forward_deprecated

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules (e.g. LlamaRMSNorm or LlamaMLP)

        # get the base model from the model instance
        base_model: LlamaModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_smollm3(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace SmolLM3 model

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.smollm3 import modeling_smollm3
    from transformers.models.smollm3.modeling_smollm3 import SmolLM3Model

    if rope:
        modeling_smollm3.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_smollm3.SmolLM3RMSNorm = LigerRMSNorm
    if swiglu:
        modeling_smollm3.SmolLM3MLP = LigerSwiGLUMLP

    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_smollm3.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(smollm3_lce_forward, model)
        else:
            modeling_smollm3.SmolLM3ForCausalLM.forward = smollm3_lce_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules (e.g. SmolLM3RMSNorm or SmolLM3MLP)

        # get the base model from the model instance
        base_model: SmolLM3Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_llava(
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    model: PreTrainedModel = None,
    **kwargs,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Llava models.
    Due to the characteristics of LlaVa, the model must be passed to apply Liger-Kernel's patch to other models connected to LLaVa.
    However, if an LM not supported by Liger-Kernel is connected to LLaVa, unexpected side effects may occur.
    NOTE: Llava is not available in transformers<4.36.0

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.llava import modeling_llava

    if cross_entropy:
        logger.warning(TRANSFORMER_DEPRECATION_WARNING)
        modeling_llava.nn.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse("4.52.0"):
            if model is not None:
                model.forward = MethodType(llava_lce_forward, model)
            else:
                modeling_llava.LlavaForConditionalGeneration.forward = llava_lce_forward
        elif transformer_version >= version.parse("4.49.0") and transformer_version < version.parse("4.52.0"):
            if model is not None:
                model.forward = MethodType(llava_lce_forward_deprecated, model)
            else:
                modeling_llava.LlavaForConditionalGeneration.forward = llava_lce_forward_deprecated
        else:  # if version < 4.49.0
            logger.warning(
                "The latest version of Liger does not support transformers < 4.49.0 for llava. Please downgrade your liger version or upgrade your transformer version."
            )

    if model is not None:
        text_model_name, vision_model_name = model.config.text_config.model_type, model.config.vision_config.model_type
        text_liger_fn = MODEL_TYPE_TO_APPLY_LIGER_FN.get(text_model_name, None)
        vision_liger_fn = MODEL_TYPE_TO_APPLY_LIGER_FN.get(vision_model_name, None)

        kwargs = {"cross_entropy": False, "fused_linear_cross_entropy": False, **kwargs}
        if text_liger_fn:
            accept_params = inspect.signature(text_liger_fn).parameters
            remain_params = set(kwargs) - (set(accept_params) & set(kwargs))
            text_kwargs = {k: v for k, v in kwargs.items() if k not in remain_params}

            if remain_params:
                logger.warning(
                    f"These parameters are not supported by {text_model_name}. Enter the remaining {list(text_kwargs.keys())} except for {list(remain_params)}\n"
                    f"Parameters accepted by {text_model_name}: {list(accept_params.keys())}"
                )
            text_kwargs["model"] = model.language_model
            text_liger_fn(**text_kwargs)
        elif text_model_name not in MODEL_TYPE_TO_APPLY_LIGER_FN:
            logger.warning(f"{text_model_name} is not supported by Liger kernel.")

        if vision_liger_fn:
            accept_params = inspect.signature(vision_liger_fn).parameters
            remain_params = set(kwargs) - (set(accept_params) & set(kwargs))
            vision_kwargs = {k: v for k, v in kwargs.items() if k not in remain_params}

            if remain_params:
                logger.warning(
                    f"These parameters are not supported by {vision_model_name}. Enter the remaining {list(vision_kwargs.keys())} except for {list(remain_params)}\n"
                    f"Parameters accepted by {vision_model_name}: {list(accept_params.keys())}"
                )
            vision_kwargs["model"] = model.vision_tower
            vision_liger_fn(**vision_kwargs)
        elif vision_model_name not in MODEL_TYPE_TO_APPLY_LIGER_FN:
            logger.warning(f"{vision_model_name} is not supported by Liger kernel.")


def apply_liger_kernel_to_llama4(
    rope: bool = False,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
    layer_norm: bool = True,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Llama4 models.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is False.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.llama4 import modeling_llama4
    from transformers.models.llama4.modeling_llama4 import Llama4ForCausalLM
    from transformers.models.llama4.modeling_llama4 import Llama4ForConditionalGeneration
    from transformers.models.llama4.modeling_llama4 import Llama4TextModel
    from transformers.models.llama4.modeling_llama4 import Llama4VisionModel

    from liger_kernel.transformers.model.llama4 import lce_forward as llama4_lce_forward

    if rope:
        raise NotImplementedError("liger_rotary_pos_emb is not available for Llama4 models.")
    if rms_norm:
        modeling_llama4.Llama4TextRMSNorm = LigerRMSNorm
    if swiglu:
        modeling_llama4.Llama4TextMLP = LigerSwiGLUMLP

    if cross_entropy:
        modeling_llama4.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        modeling_llama4.Llama4ForCausalLM.forward = llama4_lce_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules
        if isinstance(model, Llama4ForConditionalGeneration):
            language_model: Llama4ForCausalLM = model.language_model
            vision_model: Llama4VisionModel = model.vision_model
            text_model: Llama4TextModel = language_model.model
        elif isinstance(model, Llama4ForCausalLM):
            text_model = model.model
            vision_model = None
        elif isinstance(model, Llama4TextModel):
            text_model = model
            vision_model = None

        else:
            raise ValueError(f"Unsupported Llama4 model type: {type(model)}")

        if text_model:
            if rms_norm:
                _patch_rms_norm_module(text_model.norm)
            for decoder_layer in text_model.layers:
                if swiglu:
                    _patch_swiglu_module(decoder_layer.feed_forward, LigerSwiGLUMLP)
                if rms_norm:
                    _patch_rms_norm_module(decoder_layer.input_layernorm)
                    _patch_rms_norm_module(decoder_layer.post_attention_layernorm)

        if vision_model:
            _patch_layer_norm_module(vision_model.layernorm_pre)
            _patch_layer_norm_module(vision_model.layernorm_post)

            for layer in vision_model.model.layers:
                if layer_norm:
                    _patch_layer_norm_module(layer.input_layernorm)
                    _patch_layer_norm_module(layer.post_attention_layernorm)


def apply_liger_kernel_to_mllama(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    layer_norm: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace MLlama models.
    NOTE: MLlama is not available in transformers<4.45.0

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.mllama import modeling_mllama
    from transformers.models.mllama.modeling_mllama import MllamaForCausalLM
    from transformers.models.mllama.modeling_mllama import MllamaForConditionalGeneration
    from transformers.models.mllama.modeling_mllama import MllamaTextModel
    from transformers.models.mllama.modeling_mllama import MllamaVisionModel

    from liger_kernel.transformers.model.mllama import lce_forward as mllama_lce_forward
    from liger_kernel.transformers.model.mllama import lce_forward_deprecated as mllama_lce_forward_deprecated

    if rope:
        modeling_mllama.apply_rotary_pos_emb = liger_rotary_pos_emb
    if layer_norm and model is None:
        modeling_mllama.nn.LayerNorm = LigerLayerNorm
    if rms_norm:
        modeling_mllama.MllamaTextRMSNorm = LigerRMSNorm
    if swiglu:
        modeling_mllama.MllamaTextMLP = LigerSwiGLUMLP
    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_mllama.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(mllama_lce_forward, model)
            else:
                modeling_mllama.MllamaForCausalLM.forward = mllama_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(mllama_lce_forward_deprecated, model)
            else:
                modeling_mllama.MllamaForCausalLM.forward = mllama_lce_forward_deprecated

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if isinstance(model, MllamaForConditionalGeneration):
            language_model: MllamaForCausalLM = model.language_model
            vision_model: MllamaVisionModel = model.vision_model
            if isinstance(language_model, MllamaForCausalLM):
                text_model: MllamaTextModel = language_model.model
            else:
                text_model = language_model
        elif isinstance(model, MllamaForCausalLM):
            text_model = model.model
            vision_model = None
        elif isinstance(model, MllamaTextModel):
            text_model = model
            vision_model = None

        else:
            raise ValueError(f"Unsupported Mllama model type: {type(model)}")

        if text_model:
            if rms_norm:
                _patch_rms_norm_module(text_model.norm)
            for decoder_layer in text_model.layers:
                if swiglu:
                    _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
                if rms_norm:
                    _patch_rms_norm_module(decoder_layer.input_layernorm)
                    _patch_rms_norm_module(decoder_layer.post_attention_layernorm)

        if vision_model:
            _patch_layer_norm_module(vision_model.layernorm_pre)
            _patch_layer_norm_module(vision_model.layernorm_post)

            for layer in vision_model.transformer.layers:
                if layer_norm:
                    _patch_layer_norm_module(layer.input_layernorm)
                    _patch_layer_norm_module(layer.post_attention_layernorm)

            for layer in vision_model.global_transformer.layers:
                if layer_norm:
                    _patch_layer_norm_module(layer.input_layernorm)
                    _patch_layer_norm_module(layer.post_attention_layernorm)


def apply_liger_kernel_to_mistral(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Mistral models

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is False.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is True.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.mistral import modeling_mistral
    from transformers.models.mistral.modeling_mistral import MistralModel

    if rope:
        modeling_mistral.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_mistral.MistralRMSNorm = LigerRMSNorm
    if cross_entropy:
        modeling_mistral.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse("4.49.0"):
            if model is not None:
                model.forward = MethodType(mistral_lce_forward, model)
            else:
                modeling_mistral.MistralForCausalLM.forward = mistral_lce_forward
        else:
            logger.warning(
                "The latest version of Liger does not support transformers < 4.49.0 for llava. Please downgrade your liger version or upgrade your transformer version."
            )
            logger.warning("LigerFusedLinearCrossEntropy patch is not applied.")

    if swiglu:
        modeling_mistral.MistralMLP = LigerSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: MistralModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_mixtral(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Mixtral models

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.mixtral import modeling_mixtral
    from transformers.models.mixtral.modeling_mixtral import MixtralModel

    if rope:
        modeling_mixtral.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_mixtral.MixtralRMSNorm = LigerRMSNorm
    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_mixtral.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(mixtral_lce_forward, model)
            else:
                modeling_mixtral.MixtralForCausalLM.forward = mixtral_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(mixtral_lce_forward_deprecated, model)
            else:
                modeling_mixtral.MixtralForCausalLM.forward = mixtral_lce_forward_deprecated
    if swiglu:
        modeling_mixtral.MixtralBlockSparseTop2MLP = LigerBlockSparseTop2MLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: MixtralModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                for expert in decoder_layer.block_sparse_moe.experts:
                    _patch_swiglu_module(expert, LigerBlockSparseTop2MLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_gemma(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    geglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Gemma
    (Gemma 1 and 1.1 supported, for Gemma2 please use `apply_liger_kernel_to_gemma2` ) to make GPU go burrr.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        geglu (bool): Whether to apply Liger's GeGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.gemma import modeling_gemma
    from transformers.models.gemma.modeling_gemma import GemmaModel

    from liger_kernel.transformers.rms_norm import LigerRMSNormForGemma

    _patch_rms_norm_module_for_gemma = partial(_patch_rms_norm_module, casting_mode="gemma", offset=1.0)

    if rope:
        modeling_gemma.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_gemma.GemmaRMSNorm = LigerRMSNormForGemma
    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_gemma.CrossEntropyLoss = LigerCrossEntropyLoss
    if geglu:
        modeling_gemma.GemmaMLP = LigerGEGLUMLP
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(gemma_lce_forward, model)
            else:
                modeling_gemma.GemmaForCausalLM.forward = gemma_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(gemma_lce_forward_deprecated, model)
            else:
                modeling_gemma.GemmaForCausalLM.forward = gemma_lce_forward_deprecated

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: GemmaModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module_for_gemma(base_model.norm)

        for decoder_layer in base_model.layers:
            if geglu:
                _patch_geglu_module(decoder_layer.mlp)
            if rms_norm:
                _patch_rms_norm_module_for_gemma(decoder_layer.input_layernorm)
                _patch_rms_norm_module_for_gemma(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_gemma2(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    geglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Gemma2
    (for Gemma1 please use `apply_liger_kernel_to_gemma`) to make GPU go burrr.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        geglu (bool): Whether to apply Liger's GeGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.gemma2 import modeling_gemma2
    from transformers.models.gemma2.modeling_gemma2 import Gemma2Model

    from liger_kernel.transformers.rms_norm import LigerRMSNormForGemma2

    _patch_rms_norm_module_for_gemma2 = partial(
        _patch_rms_norm_module, offset=1.0, casting_mode="gemma", in_place=False
    )

    if rope:
        modeling_gemma2.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        # https://github.com/huggingface/transformers/blob/v4.44.2/src/transformers/models/gemma/modeling_gemma.py#L109
        modeling_gemma2.Gemma2RMSNorm = LigerRMSNormForGemma2
    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_gemma2.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(gemma2_lce_forward, model)
            else:
                modeling_gemma2.Gemma2ForCausalLM.forward = gemma2_lce_forward
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(gemma2_lce_forward_deprected, model)
            else:
                modeling_gemma2.Gemma2ForCausalLM.forward = gemma2_lce_forward_deprected
    if geglu:
        modeling_gemma2.Gemma2MLP = LigerGEGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Gemma2Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module_for_gemma2(base_model.norm)

        for decoder_layer in base_model.layers:
            if geglu:
                _patch_geglu_module(decoder_layer.mlp)
            if rms_norm:
                _patch_rms_norm_module_for_gemma2(decoder_layer.input_layernorm)
                _patch_rms_norm_module_for_gemma2(decoder_layer.post_attention_layernorm)
                _patch_rms_norm_module_for_gemma2(decoder_layer.pre_feedforward_layernorm)
                _patch_rms_norm_module_for_gemma2(decoder_layer.post_feedforward_layernorm)


def apply_liger_kernel_to_gemma3_text(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    geglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Gemma3

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        geglu (bool): Whether to apply Liger's GeGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.gemma3 import modeling_gemma3
    from transformers.models.gemma3.modeling_gemma3 import Gemma3DecoderLayer
    from transformers.models.gemma3.modeling_gemma3 import Gemma3ForCausalLM
    from transformers.models.gemma3.modeling_gemma3 import Gemma3TextModel

    from liger_kernel.transformers.model.gemma3 import causal_forward
    from liger_kernel.transformers.rms_norm import LigerRMSNormForGemma3

    _patch_rms_norm_module_for_gemma3 = partial(
        _patch_rms_norm_module, offset=1.0, casting_mode="gemma", in_place=False
    )

    if rope:
        modeling_gemma3.apply_rotary_pos_emb = liger_rotary_pos_emb

    if rms_norm:
        modeling_gemma3.Gemma3RMSNorm = LigerRMSNormForGemma3

    if geglu:
        modeling_gemma3.Gemma3MLP = LigerGEGLUMLP

    # Handle loss function
    if cross_entropy:
        from transformers.loss.loss_utils import nn

        nn.functional.cross_entropy = liger_cross_entropy

    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(causal_forward, model)
        else:
            modeling_gemma3.Gemma3ForCausalLM.forward = causal_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if isinstance(model, Gemma3ForCausalLM) or isinstance(model, Gemma3TextModel):
            # get the base model from the model instance
            base_model = model.model if isinstance(model, Gemma3ForCausalLM) else model

            if rms_norm:
                _patch_rms_norm_module_for_gemma3(base_model.norm)

            for decoder_layer in base_model.layers:
                decoder_layer: Gemma3DecoderLayer
                if geglu:
                    _bind_method_to_module(decoder_layer.mlp, "forward", LigerGEGLUMLP.forward)
                if rms_norm:
                    _patch_rms_norm_module_for_gemma3(decoder_layer.input_layernorm)
                    _patch_rms_norm_module_for_gemma3(decoder_layer.post_attention_layernorm)
                    _patch_rms_norm_module_for_gemma3(decoder_layer.pre_feedforward_layernorm)
                    _patch_rms_norm_module_for_gemma3(decoder_layer.post_feedforward_layernorm)
                    _patch_rms_norm_module_for_gemma3(decoder_layer.self_attn.q_norm)
                    _patch_rms_norm_module_for_gemma3(decoder_layer.self_attn.k_norm)

        else:
            raise TypeError("The model must be Gemma3ForCausalLM.")


def apply_liger_kernel_to_gemma3(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    layer_norm: bool = True,
    rms_norm: bool = True,
    geglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Gemma3

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        layer_norm (bool): Whether to apply Liger's LayerNorm. Default is True.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        geglu (bool): Whether to apply Liger's GeGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.gemma3 import modeling_gemma3
    from transformers.models.gemma3.modeling_gemma3 import Gemma3ForConditionalGeneration
    from transformers.models.siglip import modeling_siglip
    from transformers.models.siglip.modeling_siglip import SiglipEncoderLayer
    from transformers.models.siglip.modeling_siglip import SiglipVisionModel

    from liger_kernel.transformers.model.gemma3 import multimodal_forward

    _patch_rms_norm_module_for_gemma3 = partial(
        _patch_rms_norm_module, offset=1.0, casting_mode="gemma", in_place=False
    )

    if layer_norm and model is None:
        modeling_siglip.nn.LayerNorm = LigerLayerNorm

    apply_liger_kernel_to_gemma3_text(
        rope=rope, cross_entropy=False, fused_linear_cross_entropy=False, rms_norm=rms_norm, geglu=geglu
    )

    if cross_entropy:
        modeling_gemma3.nn.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(multimodal_forward, model)
        else:
            modeling_gemma3.Gemma3ForConditionalGeneration.forward = multimodal_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if isinstance(model, Gemma3ForConditionalGeneration):
            if isinstance(model.vision_tower, SiglipVisionModel):
                vision_tower = model.vision_tower

                _patch_layer_norm_module(vision_tower.vision_model.post_layernorm)

                for layer in vision_tower.vision_model.encoder.layers:
                    layer: SiglipEncoderLayer
                    if layer_norm:
                        _patch_layer_norm_module(layer.layer_norm1)
                        _patch_layer_norm_module(layer.layer_norm2)
            else:
                raise TypeError("The vision tower must be SiglipVisionModel")

            if rms_norm:
                _patch_rms_norm_module_for_gemma3(model.multi_modal_projector.mm_soft_emb_norm)

            apply_liger_kernel_to_gemma3_text(
                rope=rope,
                cross_entropy=False,
                fused_linear_cross_entropy=False,
                rms_norm=rms_norm,
                geglu=geglu,
                model=model.language_model,
            )

        else:
            raise TypeError("The model must be Gemma3ForConditionalGeneration.")


def apply_liger_kernel_to_paligemma(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    layer_norm: bool = True,
    rms_norm: bool = True,
    geglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace PaliGemma

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        layer_norm (bool): Whether to apply Liger's LayerNorm. Default is True.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        geglu (bool): Whether to apply Liger's GeGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    # PaliGemma submodules are ['vision_tower', 'multi_modal_projector', 'language_model']

    from transformers.models.gemma.modeling_gemma import GemmaForCausalLM
    from transformers.models.gemma.modeling_gemma import GemmaModel
    from transformers.models.gemma2.modeling_gemma2 import Gemma2ForCausalLM
    from transformers.models.gemma2.modeling_gemma2 import Gemma2Model
    from transformers.models.paligemma import modeling_paligemma
    from transformers.models.paligemma.modeling_paligemma import PaliGemmaForConditionalGeneration
    from transformers.models.siglip import modeling_siglip
    from transformers.models.siglip.modeling_siglip import SiglipEncoderLayer
    from transformers.models.siglip.modeling_siglip import SiglipVisionModel

    from liger_kernel.transformers.model.paligemma import lce_forward
    from liger_kernel.transformers.model.paligemma import lce_forward_deprecated

    # The vision_tower is a SiglipVisionModel
    if layer_norm and model is None:
        modeling_siglip.nn.LayerNorm = LigerLayerNorm

    # SiglipMLP is standard FFN so LigerGEGLUMLP is not compatible
    # The multi_modal_projector is Linear, nothing to do

    # The language_model is GemmaForCausalLM or Gemma2ForCausalLM
    apply_liger_kernel_to_gemma(
        rope=rope, cross_entropy=False, fused_linear_cross_entropy=False, rms_norm=rms_norm, geglu=geglu
    )
    apply_liger_kernel_to_gemma2(
        rope=rope, cross_entropy=False, fused_linear_cross_entropy=False, rms_norm=rms_norm, geglu=geglu
    )
    # Handle loss function
    if cross_entropy:
        modeling_paligemma.nn.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(lce_forward, model)
            else:
                modeling_paligemma.PaliGemmaForConditionalGeneration.forward = lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(lce_forward_deprecated, model)
            else:
                modeling_paligemma.PaliGemmaForConditionalGeneration.forward = lce_forward_deprecated

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if not isinstance(model, PaliGemmaForConditionalGeneration):
            raise TypeError("model have to be of type PaliGemmaForConditionalGeneration")

        vision_tower: SiglipVisionModel = model.vision_tower

        _patch_layer_norm_module(vision_tower.vision_model.post_layernorm)

        for layer in vision_tower.vision_model.encoder.layers:
            layer: SiglipEncoderLayer
            if layer_norm:
                _patch_layer_norm_module(layer.layer_norm1)
                _patch_layer_norm_module(layer.layer_norm2)

        language_model = model.language_model

        if isinstance(language_model, (GemmaForCausalLM, GemmaModel)):
            apply_liger_kernel_to_gemma(
                rope=rope,
                cross_entropy=False,
                fused_linear_cross_entropy=False,
                rms_norm=rms_norm,
                geglu=geglu,
                model=language_model,
            )

        elif isinstance(language_model, (Gemma2ForCausalLM, Gemma2Model)):
            apply_liger_kernel_to_gemma2(
                rope=rope,
                cross_entropy=False,
                fused_linear_cross_entropy=False,
                rms_norm=rms_norm,
                geglu=geglu,
                model=language_model,
            )
        else:
            raise TypeError(
                "The language_model of a PaliGemma model must be either GemmaForCausalLM or Gemma2ForCausalLM."
            )


def apply_liger_kernel_to_qwen2(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Qwen2 models

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.qwen2 import modeling_qwen2
    from transformers.models.qwen2.modeling_qwen2 import Qwen2Model

    if rope:
        modeling_qwen2.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_qwen2.Qwen2RMSNorm = LigerRMSNorm

    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_qwen2.CrossEntropyLoss = LigerCrossEntropyLoss

    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(qwen2_lce_forward, model)
            else:
                modeling_qwen2.Qwen2ForCausalLM.forward = qwen2_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(qwen2_lce_forward_deprecated, model)
            else:
                modeling_qwen2.Qwen2ForCausalLM.forward = qwen2_lce_forward_deprecated

    if swiglu:
        modeling_qwen2.Qwen2MLP = LigerSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Qwen2Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)
    print("Applied Liger kernels to Qwen2")


def apply_liger_kernel_to_qwen3(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Qwen3 models.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.qwen3 import modeling_qwen3
    from transformers.models.qwen3.modeling_qwen3 import Qwen3Model

    from liger_kernel.transformers.model.qwen3 import lce_forward as qwen3_lce_forward

    if rope:
        modeling_qwen3.apply_rotary_pos_emb = liger_rotary_pos_emb

    if rms_norm:
        modeling_qwen3.Qwen3RMSNorm = LigerRMSNorm

    if cross_entropy:
        from transformers.loss.loss_utils import nn

        nn.functional.cross_entropy = liger_cross_entropy

    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(qwen3_lce_forward, model)
        else:
            modeling_qwen3.Qwen3ForCausalLM.forward = qwen3_lce_forward

    if swiglu:
        modeling_qwen3.Qwen3MLP = LigerSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Qwen3Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)
        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_qwen3_moe(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Qwen3 models.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.qwen3_moe import modeling_qwen3_moe
    from transformers.models.qwen3_moe.modeling_qwen3_moe import Qwen3MoeModel

    from liger_kernel.transformers.model.qwen3_moe import lce_forward as qwen3_lce_forward
    from liger_kernel.transformers.swiglu import LigerQwen3MoeSwiGLUMLP

    if rope:
        modeling_qwen3_moe.apply_rotary_pos_emb = liger_rotary_pos_emb

    if rms_norm:
        modeling_qwen3_moe.Qwen3MoeRMSNorm = LigerRMSNorm

    if cross_entropy:
        from transformers.loss.loss_utils import nn

        nn.functional.cross_entropy = liger_cross_entropy

    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(qwen3_lce_forward, model)
        else:
            modeling_qwen3_moe.Qwen3MoeForCausalLM.forward = qwen3_lce_forward

    if swiglu:
        modeling_qwen3_moe.Qwen3MoeMLP = LigerQwen3MoeSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Qwen3MoeModel = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)
        for decoder_layer in base_model.layers:
            if swiglu:
                for mlp_expert in decoder_layer.mlp.experts:
                    _patch_swiglu_module(mlp_expert, LigerQwen3MoeSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_qwen2_vl(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    layer_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Qwen2-VL models.
    NOTE: Qwen2-VL is not supported in transformers<4.52.4

    Args:
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        layer_norm (bool): Whether to apply Liger's LayerNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    if transformer_version < version.parse("4.52.4"):
        logger.warning("Qwen2-VL support is only compatible with transformers >= 4.52.4")
        return

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.qwen2_vl import modeling_qwen2_vl
    from transformers.models.qwen2_vl.modeling_qwen2_vl import Qwen2VisionTransformerPretrainedModel
    from transformers.models.qwen2_vl.modeling_qwen2_vl import Qwen2VLForConditionalGeneration
    from transformers.models.qwen2_vl.modeling_qwen2_vl import Qwen2VLModel
    from transformers.models.qwen2_vl.modeling_qwen2_vl import Qwen2VLTextModel

    from liger_kernel.transformers.model.qwen2_vl import lce_forward as qwen2_vl_lce_forward

    if rope:
        modeling_qwen2_vl.apply_multimodal_rotary_pos_emb = liger_multimodal_rotary_pos_emb
    if rms_norm:
        # https://github.com/huggingface/transformers/blob/main/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py#L439
        modeling_qwen2_vl.Qwen2RMSNorm = LigerRMSNorm
    if layer_norm and model is None:
        modeling_qwen2_vl.LayerNorm = LigerLayerNorm
    if cross_entropy:
        modeling_qwen2_vl.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(qwen2_vl_lce_forward, model)
        else:
            modeling_qwen2_vl.Qwen2VLForConditionalGeneration.forward = qwen2_vl_lce_forward
    if swiglu:
        modeling_qwen2_vl.Qwen2MLP = LigerSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if isinstance(model, (Qwen2VLForConditionalGeneration, Qwen2VLModel)):
            # Note: language_model and visual properties can be accessed throught conditional class for BC.
            # Not sure if it is subject to changes in the future.
            # Reference: https://github.com/huggingface/transformers/blob/v4.52.4/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py#L1698
            text_model: Qwen2VLTextModel = model.language_model
            vision_model: Qwen2VisionTransformerPretrainedModel = model.visual
        elif isinstance(model, Qwen2VLTextModel):
            text_model: Qwen2VLTextModel = model
            vision_model = None
        else:
            # Note: Currently there's no support for patching vision model only. Feel free to raise an issue if needed.
            raise TypeError(
                f"Unsupported Qwen2VL model type. `model` must be `Qwen2VLForConditionalGeneration`, `Qwen2VLModel` or `Qwen2VLTextModel`. Got: {type(model)}"
            )

        # Patch Qwen2VisionTransformerPretrainedModel
        if vision_model is not None:
            for vision_block in vision_model.blocks:
                if layer_norm:
                    _patch_layer_norm_module(vision_block.norm1)
                    _patch_layer_norm_module(vision_block.norm2)

        # Patch Qwen2VisionTextModel
        if text_model is not None:
            if rms_norm:
                _patch_rms_norm_module(text_model.norm)
            for decoder_layer in text_model.layers:
                if swiglu:
                    _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
                if rms_norm:
                    _patch_rms_norm_module(decoder_layer.input_layernorm)
                    _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_qwen2_5_vl(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Qwen2.5-VL models.
    NOTE: Qwen2.5-VL is not available in transformers<4.48.2

    Args:
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    if transformer_version < version.parse("4.52.4"):
        logger.warning("Qwen2.5-VL support is only compatible with transformers >= 4.52.4")
        return

    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.qwen2_5_vl import modeling_qwen2_5_vl
    from transformers.models.qwen2_5_vl.modeling_qwen2_5_vl import Qwen2_5_VisionTransformerPretrainedModel
    from transformers.models.qwen2_5_vl.modeling_qwen2_5_vl import Qwen2_5_VLForConditionalGeneration
    from transformers.models.qwen2_5_vl.modeling_qwen2_5_vl import Qwen2_5_VLModel
    from transformers.models.qwen2_5_vl.modeling_qwen2_5_vl import Qwen2_5_VLTextModel

    from liger_kernel.transformers.model.qwen2_5_vl import lce_forward as qwen2_5_vl_lce_forward

    if rope:
        modeling_qwen2_5_vl.apply_multimodal_rotary_pos_emb = liger_multimodal_rotary_pos_emb
    if rms_norm:
        modeling_qwen2_5_vl.Qwen2RMSNorm = LigerRMSNorm
    if cross_entropy:
        modeling_qwen2_5_vl.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(qwen2_5_vl_lce_forward, model)
        else:
            modeling_qwen2_5_vl.Qwen2_5_VLForConditionalGeneration.forward = qwen2_5_vl_lce_forward
    if swiglu:
        modeling_qwen2_5_vl.Qwen2MLP = LigerSwiGLUMLP

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        if isinstance(model, (Qwen2_5_VLForConditionalGeneration, Qwen2_5_VLModel)):
            # Note: language_model and visual properties can be accessed throught conditional class for BC.
            # Not sure if it is subject to changes in the future.
            # Reference: https://github.com/huggingface/transformers/blob/v4.52.4/src/transformers/models/qwen2_5_vl/modeling_qwen2_5_vl.py#L1823
            text_model: Qwen2_5_VLTextModel = model.language_model
            vision_model: Qwen2_5_VisionTransformerPretrainedModel = model.visual
        elif isinstance(model, Qwen2_5_VLTextModel):
            text_model: Qwen2_5_VLTextModel = model
            vision_model = None
        else:
            # Note: Currently there's no support for patching vision model only. Feel free to raise an issue if needed.
            raise TypeError(
                f"Unsupported Qwen2VL model type. `model` must be `Qwen2VLForConditionalGeneration`, `Qwen2VLModel` or `Qwen2VLTextModel`. Got: {type(model)}"
            )

        if vision_model is not None:
            # Patch Qwen2_5_VisionTransformerPretrainedModel
            for vision_block in model.visual.blocks:
                if rms_norm:
                    _patch_rms_norm_module(vision_block.norm1)
                    _patch_rms_norm_module(vision_block.norm2)

        if text_model is not None:
            if rms_norm:
                _patch_rms_norm_module(text_model.norm)
            for decoder_layer in text_model.layers:
                if swiglu:
                    _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
                if rms_norm:
                    _patch_rms_norm_module(decoder_layer.input_layernorm)
                    _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_phi3(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace Phi3 models.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU Phi3MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.phi3 import modeling_phi3
    from transformers.models.phi3.modeling_phi3 import Phi3Model

    if rope:
        modeling_phi3.apply_rotary_pos_emb = liger_rotary_pos_emb  # Same as Gemma
    if rms_norm:
        modeling_phi3.Phi3RMSNorm = LigerRMSNorm  # Same as Llama
    if swiglu:
        modeling_phi3.Phi3MLP = LigerPhi3SwiGLUMLP
    if cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            from transformers.loss.loss_utils import nn

            nn.functional.cross_entropy = liger_cross_entropy
        else:
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            modeling_phi3.CrossEntropyLoss = LigerCrossEntropyLoss
    if fused_linear_cross_entropy:
        if transformer_version >= version.parse(SUPPORTED_TRANSFORMER_VERSION):
            if model is not None:
                model.forward = MethodType(phi3_lce_forward, model)
            else:
                modeling_phi3.Phi3ForCausalLM.forward = phi3_lce_forward
        else:  # if version < 4.46.1
            logger.warning(TRANSFORMER_DEPRECATION_WARNING)
            if model is not None:
                model.forward = MethodType(phi3_lce_forward_deprecated, model)
            else:
                modeling_phi3.Phi3ForCausalLM.forward = phi3_lce_forward_deprecated

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Phi3Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerPhi3SwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm)


def apply_liger_kernel_to_olmo2(
    rope: bool = True,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace OLMO2 models.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is True.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU Olmo2MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.olmo2 import modeling_olmo2
    from transformers.models.olmo2.modeling_olmo2 import Olmo2Model

    from liger_kernel.transformers.model.olmo2 import lce_forward as olmo2_lce_forward
    from liger_kernel.transformers.rms_norm import LigerRMSNormForOlmo2

    if rope:
        modeling_olmo2.apply_rotary_pos_emb = liger_rotary_pos_emb
    if rms_norm:
        modeling_olmo2.Olmo2RMSNorm = LigerRMSNormForOlmo2
    if swiglu:
        modeling_olmo2.Olmo2MLP = LigerSwiGLUMLP
    if cross_entropy:
        from transformers.loss.loss_utils import nn

        nn.functional.cross_entropy = liger_cross_entropy
    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(olmo2_lce_forward, model)
        else:
            modeling_olmo2.Olmo2ForCausalLM.forward = olmo2_lce_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Olmo2Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerSwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm, in_place=False)
                _patch_rms_norm_module(decoder_layer.post_feedforward_layernorm, in_place=False)


def apply_liger_kernel_to_glm4(
    rope: bool = False,
    cross_entropy: bool = False,
    fused_linear_cross_entropy: bool = True,
    rms_norm: bool = True,
    swiglu: bool = True,
    model: PreTrainedModel = None,
) -> None:
    """
    Apply Liger kernels to replace original implementation in HuggingFace GLM-4 models.

    Args:
        rope (bool): Whether to apply Liger's rotary position embedding. Default is False.
        cross_entropy (bool): Whether to apply Liger's cross entropy loss. Default is False.
        fused_linear_cross_entropy (bool):
            Whether to apply Liger's fused linear cross entropy loss. Default is True.
            `cross_entropy` and `fused_linear_cross_entropy` cannot both be True.
            If `fused_linear_cross_entropy` is True, the logits will not be materialized but more memory efficient.
        rms_norm (bool): Whether to apply Liger's RMSNorm. Default is True.
        swiglu (bool): Whether to apply Liger's SwiGLU Glm4MLP. Default is True.
        model (PreTrainedModel): The model instance to apply Liger kernels to, if the model has already been
        loaded. Default is None.
    """
    assert not (cross_entropy and fused_linear_cross_entropy), (
        "cross_entropy and fused_linear_cross_entropy cannot both be True."
    )

    from transformers.models.glm4 import modeling_glm4
    from transformers.models.glm4.modeling_glm4 import Glm4Model

    from liger_kernel.transformers.model.glm4 import lce_forward as glm4_lce_forward
    from liger_kernel.transformers.rms_norm import LigerRMSNormForGlm4

    if rope:
        raise NotImplementedError("liger_rotary_pos_emb is not available for Glm4 models.")
    if rms_norm:
        modeling_glm4.Glm4RMSNorm = LigerRMSNormForGlm4
    if swiglu:
        modeling_glm4.Glm4MLP = LigerPhi3SwiGLUMLP
    if cross_entropy:
        from transformers.loss.loss_utils import nn

        nn.functional.cross_entropy = liger_cross_entropy
    if fused_linear_cross_entropy:
        if model is not None:
            model.forward = MethodType(glm4_lce_forward, model)
        else:
            modeling_glm4.Glm4ForCausalLM.forward = glm4_lce_forward

    if model is not None:
        # The model instance already exists, so we need to additionally patch the
        # instance variables that reference already-instantiated modules

        # get the base model from the model instance
        base_model: Glm4Model = getattr(model, model.base_model_prefix, model)

        if rms_norm:
            _patch_rms_norm_module(base_model.norm, in_place=False)

        for decoder_layer in base_model.layers:
            if swiglu:
                _patch_swiglu_module(decoder_layer.mlp, LigerPhi3SwiGLUMLP)
            if rms_norm:
                _patch_rms_norm_module(decoder_layer.input_layernorm, in_place=False)
                _patch_rms_norm_module(decoder_layer.post_attention_layernorm, in_place=False)
                _patch_rms_norm_module(decoder_layer.post_self_attn_layernorm, in_place=False)
                _patch_rms_norm_module(decoder_layer.post_mlp_layernorm, in_place=False)


# Model type corresponds to the keys defined in transformers/models/auto/modeling_auto.py
MODEL_TYPE_TO_APPLY_LIGER_FN = {
    "gemma": apply_liger_kernel_to_gemma,
    "gemma2": apply_liger_kernel_to_gemma2,
    "gemma3_text": apply_liger_kernel_to_gemma3_text,
    "gemma3": apply_liger_kernel_to_gemma3,
    "glm4": apply_liger_kernel_to_glm4,
    "llama": apply_liger_kernel_to_llama,
    "llama4_text": apply_liger_kernel_to_llama4,
    "llama4": apply_liger_kernel_to_llama4,
    "llava": apply_liger_kernel_to_llava,
    "granite": apply_liger_kernel_to_granite,
    "mllama": apply_liger_kernel_to_mllama,
    "mllama_text_model": apply_liger_kernel_to_mllama,
    "mistral": apply_liger_kernel_to_mistral,
    "mixtral": apply_liger_kernel_to_mixtral,
    "olmo2": apply_liger_kernel_to_olmo2,
    "qwen2": apply_liger_kernel_to_qwen2,
    "qwen3": apply_liger_kernel_to_qwen3,
    "qwen3_moe": apply_liger_kernel_to_qwen3_moe,
    "qwen2_vl": apply_liger_kernel_to_qwen2_vl,
    "qwen2_vl_text": apply_liger_kernel_to_qwen2_vl,
    "qwen2_5_vl": apply_liger_kernel_to_qwen2_5_vl,
    "qwen2_5_vl_text": apply_liger_kernel_to_qwen2_5_vl,
    "smollm3": apply_liger_kernel_to_smollm3,
    "phi3": apply_liger_kernel_to_phi3,
    "paligemma": apply_liger_kernel_to_paligemma,
}


def _apply_liger_kernel(model_type: str, **kwargs) -> None:
    """
    Applies Liger kernels based on the specified model type. The custom
    kernels for the specified model type will be applied with the provided
    keyword arguments, otherwise the default configuration will be used.

    ** Note: Calling _apply_liger_kernel() after model initialization
    will not be able to fully patch models. This must be called before model initialization.
    If the model has already been instantiated

    Args:
        - model_type: the model types as defined in transformers/models/auto/modeling_auto.py
          and specified in the model's config.json
        - kwargs: keyword arguments that are passed to the corresponding apply_liger_kernel_to_* function.
    """
    if not model_type:
        logger.info("Model type was not provided. No Liger kernels will be applied.")
        return

    if model_type not in MODEL_TYPE_TO_APPLY_LIGER_FN.keys():
        logger.info(f"There are currently no Liger kernels supported for model type: {model_type}.")
        return

    apply_fn = MODEL_TYPE_TO_APPLY_LIGER_FN[model_type]
    apply_fn_signature = inspect.signature(apply_fn)

    # Filter out the keyword arguments that are not supported by the apply function
    applicable_kwargs = {key: value for key, value in kwargs.items() if key in apply_fn_signature.parameters}

    logger.info(f"Applying Liger kernels for model type: {model_type} with kwargs: {applicable_kwargs}")

    # Assume this is invoked pre-model initialization, so we only need to patch transformers code
    apply_fn(**applicable_kwargs)


def _apply_liger_kernel_to_instance(model: PreTrainedModel, **kwargs) -> None:
    """
    Applies Liger kernels to the provided model instance.

    Args:
        - model: the model instance to apply Liger kernels to
        - kwargs: keyword arguments that are passed to the corresponding apply_liger_kernel_to_* function.
    """
    model_type = getattr(model, "config", None) and getattr(model.config, "model_type", None)

    if not model_type:
        logger.info("Model type could not be determined from model config. No Liger kernels will be applied.")
        return

    if model_type not in MODEL_TYPE_TO_APPLY_LIGER_FN.keys():
        logger.info(f"There are currently no Liger kernels supported for model type: {model_type}.")
        return

    apply_fn = MODEL_TYPE_TO_APPLY_LIGER_FN[model_type]
    apply_fn_signature = inspect.signature(apply_fn)

    # Filter out the keyword arguments that are not supported by the apply function
    applicable_kwargs = {key: value for key, value in kwargs.items() if key in apply_fn_signature.parameters}
    logger.info(
        f"Applying Liger kernels to model instance with model type: {model_type} with kwargs: {applicable_kwargs}"
    )

    apply_fn(model=model, **applicable_kwargs)
