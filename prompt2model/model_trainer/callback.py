"""The real evaluation will be conduted after each mock evaluation of Trainer."""

import logging

from transformers import TrainerCallback

from prompt2model.model_evaluator import Seq2SeqEvaluator
from prompt2model.model_executor import GenerationModelExecutor


class ValidationCallback(TrainerCallback):
    """The real evaluation will be conduted after each mock evaluation of Trainer."""

    def __init__(self, trainer, tokenizer, val_dataset) -> None:
        """Initializes a new instance of Model Trainer Callback.

        Args:
            trainer: Trainer instance.
                After each epoch of Training, this callback will be called.
            tokenizer: Tokenizer to initialize model executor.
            val_dataset: Validation dataset to be evaluated on.
        """
        super().__init__()
        self.trainer = trainer
        self.tokenizer = tokenizer
        self.val_dataset = val_dataset
        self.epoch_count = 0
        self.val_dataset_size = len(self.val_dataset)

    def on_epoch_end(self, args, state, control, **kwargs):
        """After each  evaluation, this function will be called."""
        _ = (args, state, control, kwargs)
        # Pass the unused paramerters warning.
        self.epoch_count += 1
        logging.info(
            f"Epoch: {self.epoch_count}. Evaluate on { self.val_dataset_size} examples."
        )
        # For multi-GPU training, the training processor will be segmented
        # into multi-threads with data paralyzation, so the validation dataset
        # used in the callback is also segmented.
        model_executor = GenerationModelExecutor(
            self.trainer.model,
            self.tokenizer,
        )
        model_outputs = model_executor.make_prediction(
            self.val_dataset,
            "model_input",
        )
        evaluator = Seq2SeqEvaluator()
        metric_values = evaluator.evaluate_model(
            self.val_dataset,
            "model_output",
            model_outputs,
            encoder_model_name="xlm-roberta-base",
        )
        logging.info(metric_values)