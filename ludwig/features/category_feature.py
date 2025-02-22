#! /usr/bin/env python
# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
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
# ==============================================================================
import logging

import numpy as np
from typing import Dict
import torch

from ludwig.constants import *
from ludwig.decoders.generic_decoders import Classifier
from ludwig.encoders.category_encoders import ENCODER_REGISTRY
from ludwig.features.base_feature import InputFeature
from ludwig.features.base_feature import OutputFeature
from ludwig.modules.loss_modules import SoftmaxCrossEntropyLoss
# from ludwig.modules.loss_modules import SampledSoftmaxCrossEntropyLoss
from ludwig.modules.metric_modules import SoftmaxCrossEntropyMetric, CategoryAccuracy, HitsAtKMetric
# from ludwig.modules.metric_modules import SampledSoftmaxCrossEntropyMetric
from ludwig.utils.eval_utils import ConfusionMatrix
from ludwig.utils import output_feature_utils
from ludwig.utils.math_utils import int_type
from ludwig.utils.math_utils import softmax
from ludwig.utils.misc_utils import set_default_value
from ludwig.utils.misc_utils import set_default_values
from ludwig.utils.strings_utils import UNKNOWN_SYMBOL
from ludwig.utils.strings_utils import create_vocabulary

logger = logging.getLogger(__name__)


class CategoryFeatureMixin:
    type = CATEGORY
    preprocessing_defaults = {
        'most_common': 10000,
        'lowercase': False,
        'missing_value_strategy': FILL_WITH_CONST,
        'fill_value': UNKNOWN_SYMBOL
    }

    preprocessing_schema = {
        'most_common': {'type': 'integer', 'minimum': 0},
        'lowercase': {'type': 'boolean'},
        'missing_value_strategy': {'type': 'string', 'enum': MISSING_VALUE_STRATEGY_OPTIONS},
        'fill_value': {'type': 'string'},
        'computed_fill_value': {'type': 'string'},
    }

    @staticmethod
    def cast_column(column, backend):
        return column

    @staticmethod
    def get_feature_meta(column, preprocessing_parameters, backend):
        column = column.astype(str)
        idx2str, str2idx, str2freq, _, _, _, _ = create_vocabulary(
            column, 'stripped',
            num_most_frequent=preprocessing_parameters['most_common'],
            lowercase=preprocessing_parameters['lowercase'],
            add_padding=False,
            processor=backend.df_engine
        )
        return {
            'idx2str': idx2str,
            'str2idx': str2idx,
            'str2freq': str2freq,
            'vocab_size': len(str2idx)
        }

    @staticmethod
    def feature_data(column, metadata):
        return column.map(
            lambda x: (
                metadata['str2idx'][x.strip()]
                if x.strip() in metadata['str2idx']
                else metadata['str2idx'][UNKNOWN_SYMBOL]
            )
        ).astype(int_type(metadata['vocab_size']))

    @staticmethod
    def add_feature_data(
            feature,
            input_df,
            proc_df,
            metadata,
            preprocessing_parameters,
            backend,
            skip_save_processed_input
    ):
        proc_df[feature[PROC_COLUMN]] = CategoryFeatureMixin.feature_data(
            input_df[feature[COLUMN]].astype(str),
            metadata[feature[NAME]],
        )

        return proc_df


class CategoryInputFeature(CategoryFeatureMixin, InputFeature):
    encoder = 'dense'

    def __init__(self, feature, encoder_obj=None):
        super().__init__(feature)
        self.overwrite_defaults(feature)
        if encoder_obj:
            self.encoder_obj = encoder_obj
        else:
            self.encoder_obj = self.initialize_encoder(feature)

    def forward(self, inputs):
        assert isinstance(inputs, torch.Tensor)
        assert inputs.dtype == torch.int8 or inputs.dtype == torch.int16 or \
            inputs.dtype == torch.int32 or inputs.dtype == torch.int64
        assert len(inputs.shape) == 1 or (
            len(inputs.shape) == 2 and inputs.shape[1] == 1)

        if len(inputs.shape) == 1:
            inputs = inputs.unsqueeze(dim=1)

        if inputs.dtype == torch.int8 or inputs.dtype == torch.int16:
            inputs = inputs.type(torch.IntTensor)
        encoder_output = self.encoder_obj(inputs)

        return {'encoder_output': encoder_output}

    @property
    def input_dtype(self):
        return torch.int32

    @property
    def input_shape(self) -> torch.Size:
        return torch.Size([1])

    @property
    def output_shape(self) -> torch.Size:
        return torch.Size(self.encoder_obj.output_shape)

    @staticmethod
    def update_config_with_metadata(
            input_feature,
            feature_metadata,
            *args,
            **kwargs
    ):
        input_feature['vocab'] = feature_metadata['idx2str']

    @staticmethod
    def populate_defaults(input_feature):
        set_default_value(input_feature, TIED, None)

    encoder_registry = ENCODER_REGISTRY


class CategoryOutputFeature(CategoryFeatureMixin, OutputFeature):
    decoder = 'classifier'
    loss = {TYPE: SOFTMAX_CROSS_ENTROPY}
    metric_functions = {LOSS: None, ACCURACY: None, HITS_AT_K: None}
    default_validation_metric = ACCURACY
    num_classes = 0
    top_k = 3

    def __init__(self, feature):
        super().__init__(feature)
        self.overwrite_defaults(feature)
        self.decoder_obj = self.initialize_decoder(feature)
        self._setup_loss()
        self._setup_metrics()

    def logits(
            self,
            inputs,  # hidden
            **kwargs
    ):
        hidden = inputs[HIDDEN]

        # EXPECTED SHAPES FOR RETURNED TENSORS
        # logits: shape [batch_size, num_classes]
        # hidden: shape [batch_size, size of final fully connected layer]
        return {
            LOGITS: self.decoder_obj(hidden),
            PROJECTION_INPUT: hidden
        }

    def predictions(
            self,
            inputs: Dict[str, torch.Tensor],
            feature_name: str,
            **kwargs
    ):
        logits = output_feature_utils.get_output_feature_tensor(
            inputs, feature_name, LOGITS)
        probabilities = torch.softmax(logits, -1)
        predictions = torch.argmax(logits, -1)
        predictions = predictions.long()

        # EXPECTED SHAPE OF RETURNED TENSORS
        # predictions: [batch_size]
        # probabilities: [batch_size, num_classes]
        # logits: [batch_size, num_classes]
        return {
            PREDICTIONS: predictions,
            PROBABILITIES: probabilities,
            LOGITS: logits
        }

    def get_prediction_set(self):
        return {
            PREDICTIONS, PROBABILITIES, LOGITS
        }

    @property
    def input_shape(self) -> torch.Size:
        return torch.Size([self.input_size])

    @classmethod
    def get_output_dtype(cls):
        return torch.int64

    @property
    def output_shape(self) -> torch.Size:
        return torch.Size([1])

    def _setup_loss(self):
        if self.loss[TYPE] == 'softmax_cross_entropy':
            self.train_loss_function = SoftmaxCrossEntropyLoss()
        elif self.loss[TYPE] == 'sampled_softmax_cross_entropy':
            self.train_loss_function = SampledSoftmaxCrossEntropyLoss()
        else:
            raise ValueError(
                "Loss type {} is not supported. Valid values are "
                "'softmax_cross_entropy' or "
                "'sampled_softmax_cross_entropy'".format(self.loss[TYPE])
            )

        self.eval_loss_function = SoftmaxCrossEntropyLoss()

    def _setup_metrics(self):
        self.metric_functions = {}  # needed to shadow class variable
        # softmax_cross_entropy loss metric
        self.metric_functions[LOSS] = SoftmaxCrossEntropyMetric()
        self.metric_functions[ACCURACY] = CategoryAccuracy()
        if self.decoder_obj.num_classes > self.top_k:
            # Required by torchmetrics.
            self.metric_functions[HITS_AT_K] = HitsAtKMetric(top_k=self.top_k)

    @staticmethod
    def update_config_with_metadata(
            output_feature,
            feature_metadata,
            *args,
            **kwargs
    ):
        output_feature['num_classes'] = feature_metadata['vocab_size']
        output_feature['top_k'] = min(
            output_feature['num_classes'],
            output_feature['top_k']
        )

        if isinstance(output_feature[LOSS]['class_weights'], (list, tuple)):
            if (len(output_feature[LOSS]['class_weights']) !=
                    output_feature['num_classes']):
                raise ValueError(
                    'The length of class_weights ({}) is not compatible with '
                    'the number of classes ({}) for feature {}. '
                    'Check the metadata JSON file to see the classes '
                    'and their order and consider there needs to be a weight '
                    'for the <UNK> class too.'.format(
                        len(output_feature[LOSS]['class_weights']),
                        output_feature['num_classes'],
                        output_feature[COLUMN]
                    )
                )

        if isinstance(output_feature[LOSS]['class_weights'], dict):
            if (
                    feature_metadata['str2idx'].keys() !=
                    output_feature[LOSS]['class_weights'].keys()
            ):
                raise ValueError(
                    'The class_weights keys ({}) are not compatible with '
                    'the classes ({}) of feature {}. '
                    'Check the metadata JSON file to see the classes '
                    'and consider there needs to be a weight '
                    'for the <UNK> class too.'.format(
                        output_feature[LOSS]['class_weights'].keys(),
                        feature_metadata['str2idx'].keys(),
                        output_feature[COLUMN]
                    )
                )
            else:
                class_weights = output_feature[LOSS]['class_weights']
                idx2str = feature_metadata['idx2str']
                class_weights_list = [class_weights[s] for s in idx2str]
                output_feature[LOSS]['class_weights'] = class_weights_list

        if output_feature[LOSS]['class_similarities_temperature'] > 0:
            if 'class_similarities' in output_feature[LOSS]:
                similarities = output_feature[LOSS]['class_similarities']
                temperature = output_feature[LOSS][
                    'class_similarities_temperature']

                curr_row = 0
                first_row_length = 0
                is_first_row = True
                for row in similarities:
                    if is_first_row:
                        first_row_length = len(row)
                        is_first_row = False
                        curr_row += 1
                    else:
                        curr_row_length = len(row)
                        if curr_row_length != first_row_length:
                            raise ValueError(
                                'The length of row {} of the class_similarities '
                                'of {} is {}, different from the length of '
                                'the first row {}. All rows must have '
                                'the same length.'.format(
                                    curr_row,
                                    output_feature[COLUMN],
                                    curr_row_length,
                                    first_row_length
                                )
                            )
                        else:
                            curr_row += 1
                all_rows_length = first_row_length

                if all_rows_length != len(similarities):
                    raise ValueError(
                        'The class_similarities matrix of {} has '
                        '{} rows and {} columns, '
                        'their number must be identical.'.format(
                            output_feature[COLUMN],
                            len(similarities),
                            all_rows_length
                        )
                    )

                if all_rows_length != output_feature['num_classes']:
                    raise ValueError(
                        'The size of the class_similarities matrix of {} is '
                        '{}, different from the number of classe ({}). '
                        'Check the metadata JSON file to see the classes '
                        'and their order and '
                        'consider <UNK> class too.'.format(
                            output_feature[COLUMN],
                            all_rows_length,
                            output_feature['num_classes']
                        )
                    )

                similarities = np.array(similarities, dtype=np.float32)
                for i in range(len(similarities)):
                    similarities[i, :] = softmax(
                        similarities[i, :],
                        temperature=temperature
                    )

                output_feature[LOSS]['class_similarities'] = similarities
            else:
                raise ValueError(
                    'class_similarities_temperature > 0, '
                    'but no class_similarities are provided '
                    'for feature {}'.format(output_feature[COLUMN])
                )

        if output_feature[LOSS][TYPE] == 'sampled_softmax_cross_entropy':
            output_feature[LOSS]['class_counts'] = [
                feature_metadata['str2freq'][cls]
                for cls in feature_metadata['idx2str']
            ]

    @staticmethod
    def calculate_overall_stats(
            predictions,
            targets,
            train_set_metadata
    ):
        overall_stats = {}
        confusion_matrix = ConfusionMatrix(
            targets,
            predictions[PREDICTIONS],
            labels=train_set_metadata['idx2str']
        )
        overall_stats['confusion_matrix'] = confusion_matrix.cm.tolist()
        overall_stats['overall_stats'] = confusion_matrix.stats()
        overall_stats['per_class_stats'] = confusion_matrix.per_class_stats()

        return overall_stats

    def postprocess_predictions(
            self,
            predictions,
            metadata,
            output_directory,
            backend,
    ):
        predictions_col = f'{self.feature_name}_{PREDICTIONS}'
        if predictions_col in predictions:
            if 'idx2str' in metadata:
                predictions[predictions_col] = backend.df_engine.map_objects(
                    predictions[predictions_col],
                    lambda pred: metadata['idx2str'][pred]
                )

        probabilities_col = f'{self.feature_name}_{PROBABILITIES}'
        if probabilities_col in predictions:
            prob_col = f'{self.feature_name}_{PROBABILITY}'
            predictions[prob_col] = predictions[probabilities_col].map(max)
            predictions[probabilities_col] = backend.df_engine.map_objects(
                predictions[probabilities_col],
                lambda pred: pred.tolist()
            )
            if 'idx2str' in metadata:
                for i, label in enumerate(metadata['idx2str']):
                    key = f'{probabilities_col}_{label}'

                    # Use default param to force a capture before the loop completes, see:
                    # https://stackoverflow.com/questions/2295290/what-do-lambda-function-closures-capture
                    predictions[key] = backend.df_engine.map_objects(
                        predictions[probabilities_col],
                        lambda prob, i=i: prob[i],
                    )

        top_k_col = f'{self.feature_name}_predictions_top_k'
        if top_k_col in predictions:
            if 'idx2str' in metadata:
                predictions[top_k_col] = backend.df_engine.map_objects(
                    predictions[top_k_col],
                    lambda pred_top_k: [
                        metadata['idx2str'][pred] for pred in pred_top_k
                    ]
                )

        return predictions

    @staticmethod
    def populate_defaults(output_feature):
        # If Loss is not defined, set an empty dictionary
        set_default_value(output_feature, LOSS, {})

        # Populate the default values for LOSS if they aren't defined already
        set_default_values(
            output_feature[LOSS],
            {
                TYPE: 'softmax_cross_entropy',
                'labels_smoothing': 0,
                'class_weights': 1,
                'robust_lambda': 0,
                'confidence_penalty': 0,
                'class_similarities_temperature': 0,
                'weight': 1
            }
        )

        if output_feature[LOSS][TYPE] == 'sampled_softmax_cross_entropy':
            set_default_values(
                output_feature[LOSS],
                {
                    'sampler': 'log_uniform',
                    'unique': False,
                    'negative_samples': 25,
                    'distortion': 0.75
                }
            )

        set_default_values(
            output_feature,
            {
                'top_k': 3,
                'dependencies': [],
                'reduce_input': SUM,
                'reduce_dependencies': SUM
            }
        )

    decoder_registry = {
        'classifier': Classifier,
        'null': Classifier,
        'none': Classifier,
        'None': Classifier,
        None: Classifier
    }
