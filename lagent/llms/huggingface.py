import copy
import logging
import warnings
from typing import Dict, List, Optional, Union

from lagent.schema import ModelStatusCode
from .base_llm import BaseModel

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


class HFTransformer(BaseModel):
    """Model wrapper around HuggingFace general models.

    Adapted from Internlm (https://github.com/InternLM/InternLM/blob/main/
        chat/web_demo.py)

    Args:
        path (str): The name or path to HuggingFace's model.
        max_seq_len (int): The maximum length of the input sequence. Defaults
            to 2048.
        tokenizer_path (str): The path to the tokenizer. Defaults to None.
        tokenizer_kwargs (dict): Keyword arguments for the tokenizer.
            Defaults to {}.
        tokenizer_only (bool): If True, only the tokenizer will be initialized.
            Defaults to False.
        model_kwargs (dict): Keyword arguments for the model, used in loader.
            Defaults to dict(device_map='auto').
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
    """

    def __init__(self,
                 path: str,
                 tokenizer_path: Optional[str] = None,
                 tokenizer_kwargs: dict = dict(),
                 tokenizer_only: bool = False,
                 model_kwargs: dict = dict(device_map='auto'),
                 meta_template: Optional[Dict] = None,
                 **kwargs):
        super().__init__(
            path=path,
            tokenizer_only=tokenizer_only,
            meta_template=meta_template,
            **kwargs)

        self._load_tokenizer(
            path=path,
            tokenizer_path=tokenizer_path,
            tokenizer_kwargs=tokenizer_kwargs)
        if not tokenizer_only:
            self._load_model(path=path, model_kwargs=model_kwargs)

        from transformers.generation.utils import LogitsProcessorList, StoppingCriteriaList  # noqa: E501
        self.logits_processor = LogitsProcessorList()
        self.stopping_criteria = StoppingCriteriaList()
        self.prefix_allowed_tokens_fn = None

        stop_words_id = []
        if self.gen_params.get('stop_words'):
            for sw in self.gen_params.get('stop_words'):
                stop_words_id.append(self.tokenizer(sw)['input_ids'][-1])
        self.additional_eos_token_id = stop_words_id

    def _load_tokenizer(self, path: str, tokenizer_path: Optional[str],
                        tokenizer_kwargs: dict):
        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path if tokenizer_path else path,
            trust_remote_code=True,
            **tokenizer_kwargs)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

    def _load_model(self, path: str, model_kwargs: dict):
        import torch
        from transformers import AutoModel
        model_kwargs.setdefault('torch_dtype', torch.float16)
        self.model = AutoModel.from_pretrained(
            path, trust_remote_code=True, **model_kwargs)
        self.model.eval()

    def tokenize(self, inputs: str):
        assert isinstance(inputs, str)
        inputs = self.tokenizer(
            inputs, return_tensors='pt', return_length=True)
        return inputs['input_ids'].tolist()

    def generate(
        self,
        inputs: Union[str, List[str]],
        do_sample: bool = True,
        **kwargs,
    ):
        """Return the chat completions in non-stream mode.

        Args:
            inputs (Union[str, List[str]]): input texts to be completed.
            do_sample (bool): do sampling if enabled
        Returns:
            (a list of/batched) text/chat completion
        """
        for status, chunk, _ in self.stream_generate(inputs, do_sample,
                                                     **kwargs):
            response = chunk
        return response

    def stream_generate(
        self,
        inputs: List[str],
        do_sample: bool = True,
        **kwargs,
    ):
        """Return the chat completions in stream mode.

        Args:
            inputs (Union[str, List[str]]): input texts to be completed.
            do_sample (bool): do sampling if enabled
        Returns:
            tuple(Status, str, int): status, text/chat completion,
            generated token number
        """
        import torch
        from torch import nn
        with torch.no_grad():
            batched = True
            if isinstance(inputs, str):
                inputs = [inputs]
                batched = False
            # import pdb; pdb.set_trace()
            inputs = self.tokenizer(
                inputs, padding=True, return_tensors='pt', return_length=True)
            input_length = inputs['length']
            for k, v in inputs.items():
                inputs[k] = v.cuda()
            input_ids = inputs['input_ids']
            attention_mask = inputs['attention_mask']
            batch_size = input_ids.shape[0]
            input_ids_seq_length = input_ids.shape[-1]
            generation_config = self.model.generation_config
            generation_config = copy.deepcopy(generation_config)
            new_gen_params = self.update_gen_params(**kwargs)
            generation_config.update(**new_gen_params)
            generation_config.update(**kwargs)
            model_kwargs = generation_config.to_dict()
            model_kwargs['attention_mask'] = attention_mask
            _, eos_token_id = (  # noqa: F841  # pylint: disable=W0612
                generation_config.bos_token_id,
                generation_config.eos_token_id,
            )
            if isinstance(eos_token_id, int):
                eos_token_id = [eos_token_id]
            if self.additional_eos_token_id is not None:
                eos_token_id.extend(self.additional_eos_token_id)
            eos_token_id_tensor = torch.tensor(eos_token_id).to(
                input_ids.device) if eos_token_id is not None else None
            has_default_max_length = (
                kwargs.get('max_length') is None
                and generation_config.max_length is not None)
            if (has_default_max_length
                    and generation_config.max_new_tokens is None):
                warnings.warn(
                    "Using `max_length`'s default"
                    f'({generation_config.max_length})'
                    'to control the generation length. '
                    'This behaviour is deprecated and will be removed'
                    ' from the config in v5 of Transformers -- we'
                    ' recommend using `max_new_tokens` to control the'
                    ' maximum length of the generation.',
                    UserWarning,
                )
            elif generation_config.max_new_tokens is not None:
                generation_config.max_length = (
                    generation_config.max_new_tokens + input_ids_seq_length)
                if not has_default_max_length:
                    logger.warn(  # pylint: disable=W4902
                        'Both `max_new_tokens`'
                        f'(={generation_config.max_new_tokens})'
                        'and `max_length`'
                        f'(={generation_config.max_length})'
                        ' seem to have been set.`max_new_tokens`'
                        ' will take precedence. Please refer to'
                        ' the documentation for more information. '
                        '(https://huggingface.co/docs/transformers/main/en'
                        '/main_classes/text_generation)',
                        UserWarning,
                    )

            if input_ids_seq_length >= generation_config.max_length:
                input_ids_string = 'input_ids'
                logger.warning(
                    f'Input length of {input_ids_string}'
                    f' is {input_ids_seq_length},'
                    ' but `max_length` is set to'
                    f' {generation_config.max_length}.'
                    'This can lead to unexpected behavior.'
                    ' You should consider increasing `max_new_tokens`.')

            # 2. Set generation parameters if not already defined
            logits_processor = self.logits_processor
            stopping_criteria = self.stopping_criteria

            logits_processor = self.model._get_logits_processor(
                generation_config=generation_config,
                input_ids_seq_length=input_ids_seq_length,
                encoder_input_ids=input_ids,
                prefix_allowed_tokens_fn=self.prefix_allowed_tokens_fn,
                logits_processor=logits_processor,
            )

            stopping_criteria = self.model._get_stopping_criteria(
                generation_config=generation_config,
                stopping_criteria=stopping_criteria)
            logits_warper = self.model._get_logits_warper(generation_config)

            unfinished_sequences = input_ids.new(batch_size).fill_(1)
            scores = None
            while True:
                model_inputs = self.model.prepare_inputs_for_generation(
                    input_ids, **model_kwargs)
                # forward pass to get next token
                outputs = self.model(
                    **model_inputs,
                    return_dict=True,
                    output_attentions=False,
                    output_hidden_states=False,
                )

                next_token_logits = outputs.logits[:, -1, :]

                # pre-process distribution
                next_token_scores = logits_processor(input_ids,
                                                     next_token_logits)
                next_token_scores = logits_warper(input_ids, next_token_scores)

                # sample
                probs = nn.functional.softmax(next_token_scores, dim=-1)
                if do_sample:
                    next_tokens = torch.multinomial(
                        probs, num_samples=1).squeeze(1)
                else:
                    next_tokens = torch.argmax(probs, dim=-1)

                # update generated ids, model inputs,
                # and length for next step
                input_ids = torch.cat([input_ids, next_tokens[:, None]],
                                      dim=-1)
                model_kwargs = self.model._update_model_kwargs_for_generation(  # noqa: E501
                    outputs,
                    model_kwargs,
                    is_encoder_decoder=False)
                unfinished_sequences = unfinished_sequences.mul(
                    next_tokens.tile(eos_token_id_tensor.shape[0], 1).ne(
                        eos_token_id_tensor.unsqueeze(1)).prod(dim=0))
                output_token_ids = input_ids.cpu().tolist()
                for i in range(len(output_token_ids)):
                    output_token_ids[i] = output_token_ids[i][:][
                        input_length[i]:]
                    # Find the first occurrence of
                    # an EOS token in the sequence
                    first_eos_idx = next(
                        (idx
                         for idx, token_id in enumerate(output_token_ids[i])
                         if token_id in eos_token_id), None)
                    # If an EOS token is found, only the previous
                    # part of it is retained
                    if first_eos_idx is not None:
                        output_token_ids[i] = output_token_ids[
                            i][:first_eos_idx]

                response = self.tokenizer.batch_decode(output_token_ids)
                # print(response)
                if not batched:
                    response = response[0]
                yield ModelStatusCode.STREAM_ING, response, None
                # stop when each sentence is finished,
                # or if we exceed the maximum length
                if (unfinished_sequences.max() == 0
                        or stopping_criteria(input_ids, scores)):
                    break
            yield ModelStatusCode.END, response, None

    def stream_chat(
        self,
        inputs: List[dict],
        do_sample: bool = True,
        **kwargs,
    ):
        """Return the chat completions in stream mode.

        Args:
            inputs (List[dict]): input messages to be completed.
            do_sample (bool): do sampling if enabled
        Returns:
            the text/chat completion
        """
        prompt = self.template_parser(inputs)
        yield from self.stream_generate(prompt, do_sample, **kwargs)


class HFTransformerCasualLM(HFTransformer):

    def _load_model(self, path: str, model_kwargs: dict):
        import torch
        from transformers import AutoModelForCausalLM
        model_kwargs.setdefault('torch_dtype', torch.float16)
        self.model = AutoModelForCausalLM.from_pretrained(
            path, trust_remote_code=True, **model_kwargs)
        self.model.eval()
