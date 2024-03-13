# Copyright The FMS HF Tuning Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Standard
from datetime import datetime
from typing import Dict, List, Optional, Union
import json
import os
import sys
import time

# Third Party
from peft.utils.other import fsdp_auto_wrap_policy
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    GPT2Tokenizer,
    GPTNeoXTokenizerFast,
    LlamaTokenizer,
    LlamaTokenizerFast,
    TrainerCallback,
)
from transformers.utils import logging
from trl import DataCollatorForCompletionOnlyLM, SFTTrainer
import datasets
import fire
import transformers

# Local
from tuning.config import configs, peft_config, tracker_configs
from tuning.data import tokenizer_data_utils
from tuning.trackers.tracker import Tracker
from tuning.trainercontroller import TrainerControllerCallback
from tuning.trackers.tracker_factory import get_tracker
from tuning.utils.config_utils import get_hf_peft_config
from tuning.utils.data_type_utils import get_torch_dtype

TRAINING_LOGS_FILENAME = "training_logs.jsonl"

class FileLoggingCallback(TrainerCallback):
    """Exports metrics, e.g., training loss to a file in the checkpoint directory."""

    def __init__(self, logger):
        self.logger = logger

    def on_log(self, args, state, control, logs=None, **kwargs):
        """Checks if this log contains keys of interest, e.g., loss, and if so, creates
        training_logs.jsonl in the model output dir (if it doesn't already exist),
        appends the subdict of the log & dumps the file.
        """
        # All processes get the logs from this node; only update from process 0.
        if not state.is_world_process_zero:
            return

        log_file_path = os.path.join(args.output_dir, TRAINING_LOGS_FILENAME)
        if logs is not None and "loss" in logs and "epoch" in logs:
            self._track_loss("loss", "training_loss", log_file_path, logs, state)
        elif logs is not None and "eval_loss" in logs and "epoch" in logs:
            self._track_loss("eval_loss", "validation_loss", log_file_path, logs, state)

    def _track_loss(self, loss_key, log_name, log_file, logs, state):
        try:
            # Take the subdict of the last log line; if any log_keys aren't part of this log
            # object, assume this line is something else, e.g., train completion, and skip.
            log_obj = {
                "name": log_name,
                "data": {
                    "epoch": round(logs["epoch"], 2),
                    "step": state.global_step,
                    "value": logs[loss_key],
                    "timestamp": datetime.isoformat(datetime.now()),
                },
            }
        except KeyError:
            return

        # append the current log to the jsonl file
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{json.dumps(log_obj, sort_keys=True)}\n")


def train(
    model_args: configs.ModelArguments,
    data_args: configs.DataArguments,
    train_args: configs.TrainingArguments,
    peft_config: Optional[  # pylint: disable=redefined-outer-name
        Union[peft_config.LoraConfig, peft_config.PromptTuningConfig]
    ] = None,
    trainer_controller_args: configs.TrainerControllerArguments = None,
    callbacks: Optional[List[TrainerCallback]] = None,
    tracker: Optional[Tracker] = None,
    exp_metadata: Optional[Dict] = None,
):
    """Call the SFTTrainer

    Args:
        model_args: tuning.config.configs.ModelArguments
        data_args: tuning.config.configs.DataArguments
        train_args: tuning.config.configs.TrainingArguments
        peft_config: peft_config.LoraConfig for Lora tuning | \
        peft_config.PromptTuningConfig for prompt tuning | \
        None for fine tuning
            The peft configuration to pass to trainer
        trainer_control_args: configs.TrainerControllerArguments \
            for controlling the training loop using policy rules
        callbacks: List of callbacks to attach with SFTtrainer.
        tracker: One of the available trackers in trackers.tracker_factory.REGISTERED_TRACKERS
                Initialized using tuning.trackers.tracker_factory.get_tracker
                Using configs in tuning.config.tracker_configs
        exp_metadata: Dict of key value pairs passed to train to be recoreded by the tracker.
    """

    logger = logging.get_logger("sft_trainer")

    # Validate parameters
    if (not isinstance(train_args.num_train_epochs, float)) or (
        train_args.num_train_epochs <= 0
    ):
        raise ValueError("num_train_epochs has to be an integer/float >= 1")
    if (not isinstance(train_args.gradient_accumulation_steps, int)) or (
        train_args.gradient_accumulation_steps <= 0
    ):
        raise ValueError("gradient_accumulation_steps has to be an integer >= 1")

    task_type = "CAUSAL_LM"
    additional_metrics = {}

    model_load_time = time.time()
    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=train_args.cache_dir,
        torch_dtype=get_torch_dtype(model_args.torch_dtype),
        attn_implementation="flash_attention_2" if model_args.use_flash_attn else None,
    )
    additional_metrics["model_load_time"] = time.time() - model_load_time

    peft_config = get_hf_peft_config(task_type, peft_config)

    # TODO: Move these to a config as well
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path, cache_dir=train_args.cache_dir, use_fast=True
    )

    # TODO: understand if we need to hardcode these here or just use defaults in model
    if isinstance(tokenizer, (LlamaTokenizer, LlamaTokenizerFast)):
        tokenizer.add_special_tokens(
            {
                "bos_token": "<s>",
                "eos_token": "</s>",
                "unk_token": "<unk>",
                "pad_token": "<pad>",
            }
        )
    elif isinstance(tokenizer, (GPT2Tokenizer, GPTNeoXTokenizerFast)):
        tokenizer.add_special_tokens(
            {
                "pad_token": "<pad>",
            }
        )

    # TODO: near term - how response template ids are parsed out needs to be cleaned.
    # The [2:] here applies if response template has \n prefix, it is needed to strip \n,
    # otherwise template is not found. We will create issue to clean this out after we discuss
    # data formats and collators we will support.
    response_template_ids = tokenizer.encode(
        data_args.response_template, add_special_tokens=False
    )[2:]

    max_seq_length = min(train_args.max_seq_length, tokenizer.model_max_length)
    logger.info("Max sequence length is %s", max_seq_length)
    if train_args.max_seq_length > tokenizer.model_max_length:
        logger.warning(
            "max_seq_length %s exceeds tokenizer.model_max_length \
            %s, using tokenizer.model_max_length %s",
            train_args.max_seq_length,
            tokenizer.model_max_length,
            tokenizer.model_max_length,
        )

    # TODO: we need to change this, perhaps follow what open instruct does?
    special_tokens_dict = {}
    if tokenizer.pad_token is None:
        logger.warning("PAD token set to default, missing in tokenizer")
        special_tokens_dict["pad_token"] = configs.DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        logger.warning("EOS token set to default, missing in tokenizer")
        special_tokens_dict["eos_token"] = configs.DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        logger.warning("BOS token set to default, missing in tokenizer")
        special_tokens_dict["bos_token"] = configs.DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        logger.warning("UNK token set to default, missing in tokenizer")
        special_tokens_dict["unk_token"] = configs.DEFAULT_UNK_TOKEN

    # TODO: lower priority but understand if resizing impacts inference quality and why its needed.
    # It makes sense if we manipulate tokenizer that we also save it and provide it to inference.
    tokenizer_data_utils.tokenizer_and_embedding_resize(
        special_tokens_dict=special_tokens_dict,
        tokenizer=tokenizer,
        model=model,
    )

    # Configure the collator and validate args related to packing prior to formatting the dataset
    if train_args.packing:
        logger.info("Packing is set to True")
        data_collator = None
        packing = True
    else:
        logger.info("Packing is set to False")
        if data_args.response_template is None:
            # TODO: Fix this, currently unreachable due to crashing in batch encoding tokenization
            # We should do this validation up front, then do the encoding, then handle the collator
            raise ValueError("Response template is None, needs to be set for training")
        if data_args.dataset_text_field is None:
            raise ValueError("Dataset_text_field is None, needs to be set for training")
        data_collator = DataCollatorForCompletionOnlyLM(
            response_template_ids,
            tokenizer=tokenizer,
            ignore_index=configs.IGNORE_INDEX,
        )
        packing = False

    # load the data by parsing JSON
    data_files = {"train": data_args.training_data_path}
    if data_args.validation_data_path:
        data_files["validation"] = data_args.validation_data_path

    format_dataset = lambda example: {  # pylint: disable=unnecessary-lambda-assignment
        f"{data_args.dataset_text_field}": example[f"{data_args.dataset_text_field}"]
        + tokenizer.eos_token
    }

    json_dataset = datasets.load_dataset("json", data_files=data_files)
    formatted_train_dataset = json_dataset["train"].map(format_dataset)
    logger.info("Training dataset length is %s", len(formatted_train_dataset))

    formatted_validation_dataset = None
    if data_args.validation_data_path:
        formatted_validation_dataset = json_dataset["validation"].map(format_dataset)
        logger.info(
            "Validation dataset length is %s", len(formatted_validation_dataset)
        )

    if (trainer_controller_args is not None) and (
        trainer_controller_args.trainer_controller_config_file is not None
    ):
        tc_callback = TrainerControllerCallback(
            trainer_controller_args.trainer_controller_config_file
        )
        callbacks.append(tc_callback)

    if train_args.packing:
        logger.info("Packing is set to True")
        data_collator = None
        packing = True
    else:
        logger.info("Packing is set to False")
        if data_args.response_template is None:
            logger.error(
                "Error, response template is None, needs to be set for training"
            )
            sys.exit(-1)

        if data_args.dataset_text_field is None:
            logger.error(
                "Error, dataset_text_field is None, needs to be set for training"
            )
            sys.exit(-1)

        data_collator = DataCollatorForCompletionOnlyLM(
            response_template_ids,
            tokenizer=tokenizer,
            ignore_index=configs.IGNORE_INDEX,
        )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=formatted_train_dataset,
        eval_dataset=formatted_validation_dataset,
        packing=packing,
        data_collator=data_collator,
        dataset_text_field=data_args.dataset_text_field,
        args=train_args,
        max_seq_length=max_seq_length,
        callbacks=callbacks,
        peft_config=peft_config,
    )

    # We track additional metrics and experiment metadata after
    # Trainer object creation to ensure that this is not repeated
    # multiple times for FSDP runs.
    if tracker is not None:
        # Currently tracked only on process zero.
        if trainer.is_world_process_zero():
            try:
                for k, v in additional_metrics.items():
                    tracker.track(metric=v, name=k, stage="additional_metrics")
                tracker.set_params(params=exp_metadata, name="experiment_metadata")
            except ValueError as e:
                logger.error(
                    "Exception while saving additional metrics and metadata %s", repr(e)
                )

    if trainer.is_fsdp_enabled and peft_config is not None:
        trainer.accelerator.state.fsdp_plugin.auto_wrap_policy = fsdp_auto_wrap_policy(
            model
        )
    trainer.train()


def main(**kwargs):  # pylint: disable=unused-argument
    parser = transformers.HfArgumentParser(
        dataclass_types=(
            configs.ModelArguments,
            configs.DataArguments,
            configs.TrainingArguments,
            configs.TrainerControllerArguments,
            peft_config.LoraConfig,
            peft_config.PromptTuningConfig,
            tracker_configs.AimConfig,
        )
    )
    parser.add_argument(
        "--peft_method",
        type=str.lower,
        choices=["pt", "lora", None, "none"],
        default="none",
    )
    parser.add_argument(
        "--exp_metadata",
        type=str,
        default=None,
        help='Pass a json string representing K:V pairs to be associated\
              to the tuning run in the tracker. e.g. \'{"gpu":"A100-80G"}\'',
    )
    (
        model_args,
        data_args,
        training_args,
        trainer_controller_args,
        lora_config,
        prompt_tuning_config,
        aim_config,
        additional,
        _,
    ) = parser.parse_args_into_dataclasses(return_remaining_strings=True)

    logger = logging.get_logger("__main__")

    peft_method = additional.peft_method
    if peft_method == "lora":
        tune_config = lora_config
    elif peft_method == "pt":
        tune_config = prompt_tuning_config
    else:
        tune_config = None

    tracker_name = training_args.tracker
    if tracker_name == "aim":
        tracker_config = aim_config
    else:
        tracker_config = None

    # Initialize callbacks
    file_logger_callback = FileLoggingCallback(logger)
    callbacks = [file_logger_callback]

    # Initialize the tracker
    tracker = get_tracker(tracker_name, tracker_config)
    tracker_callback = tracker.get_hf_callback()
    if tracker_callback is not None:
        callbacks.append(tracker_callback)

    # extra metadata passed via client
    metadata = None
    if additional.exp_metadata is not None:
        try:
            metadata = json.loads(additional.exp_metadata)
            if metadata is None or not isinstance(metadata, Dict):
                logger.warning(
                    "metadata cannot be converted to simple k:v dict ignoring"
                )
                metadata = None
        except ValueError as e:
            logger.error(
                "failed while parsing extra metadata. pass a valid json %s", repr(e)
            )

    train(
        model_args=model_args,
        data_args=data_args,
        train_args=training_args,
        peft_config=tune_config,
        trainer_controller_args=trainer_controller_args,
        callbacks=callbacks,
        tracker=tracker,
        exp_metadata=metadata,
    )


if __name__ == "__main__":
    fire.Fire(main)
