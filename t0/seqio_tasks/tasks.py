"""
This file defines 8 mixtures that we used in the T-Zero paper:
- t0_train: T0 training mixture
- t0+_train: T0+ training mixture
- t0++_train: T0++ training mixture
- t0_eval_score_eval: T0 main evaluation mixture (Figure 4 for instance)
- t0_train_score_eval: Evaluation mixture for checkpoint selection on T0 (validation splits of the training sets)
- t0_train_one_og_prompt: T0 (p=1) training mixture for  - one original-task prompt per dataset. Figure 6
- t0_train_all_og_prompts: T0 (p=5.7) training mixture for - all original-task prompts for all datasets. Figure 6
- bias_fairness_eval_score_eval: Bias & fairness evaluation mixture. Appendix B3
"""


import csv
import functools
from typing import Dict, List, Optional, Tuple

import datasets
import pkg_resources
from promptsource import templates
import seqio
import t5
from t5.data.glue_utils import get_glue_metric, get_super_glue_metric
from t5.evaluation import metrics as mt
import tensorflow as tf

from t0.seqio_tasks import utils

GET_METRICS = {
    "BLEU": mt.bleu,
    "ROUGE": mt.rouge,
    "Span Squad": mt.span_squad,
    "Squad": mt.squad,
    "Trivia QA": mt.trivia_qa,
    "Accuracy": mt.accuracy,
    "Sequence Accuracy": mt.sequence_accuracy,
    "Pearson Correlation": mt.pearson_corrcoef,
    "Spearman Correlation": mt.spearman_corrcoef,
    "MultiRC": mt.multirc_f1_over_all_answers,
    "AUC": mt.auc,
    "COQA F1": mt.coqa_f1,
    "Edit Distance": mt.edit_distance,
    # "Mean Reciprocal Rank": mt.accuracy,  # NOTE not in T5?
    "Other": mt.accuracy,
    # Missing support for mean_multiclass_f1 etc. which need a num_classes parameter
}

MAX_EXAMPLES_PER_DATASET = 500_000


def strip_whitespace(output_or_target, example=None, is_target=False):
    """Cached tasks from promptsource all have a leading space on the ground-truth targets."""
    return output_or_target.strip()


def maybe_get_class_id_postprocessor(template):
    if template.get_fixed_answer_choices_list():

        def postprocess_fn(output_or_target, example=None, is_target=False):
            output_or_target = strip_whitespace(output_or_target)
            return t5.data.postprocessors.string_label_to_class_id(
                output_or_target, label_classes=template.get_fixed_answer_choices_list()
            )

        return postprocess_fn

    else:
        return strip_whitespace


def get_tf_dataset(split, shuffle_files, seed=None, dataset_name=None, subset_name=None, template=None, split_mapping=None):
    # HF datasets does not support file-level shuffling
    del shuffle_files, seed
    dataset = datasets.load_dataset(dataset_name, subset_name)
    dataset = dataset[split_mapping[split]]
    dataset = utils.apply_template(dataset, template)
    return utils.hf_dataset_to_tf_dataset(dataset)


def add_task(dataset_name, subset_name, template_name, task_name=None, split_mapping=None):
    template = all_templates.get_dataset(dataset_name, subset_name)[template_name]
    task_name = task_name or utils.get_task_name(dataset_name, subset_name, template_name)

    if dataset_name == "glue":
        metrics = get_glue_metric(subset_name)
    elif dataset_name == "super_glue":
        if subset_name in ("wsc.fixed", "multirc"):
            # TODO: WSC and MultiRC need special pre/postprocesing
            metrics = [mt.accuracy]
        else:
            metrics = get_super_glue_metric(subset_name)
    else:
        # TODO what if metric is null?
        metrics = [GET_METRICS[m] for m in template.metadata.metrics]

    dataset_splits = utils.get_dataset_splits(dataset_name, subset_name)
    split_mapping = split_mapping or {k: k for k in dataset_splits.keys()}

    dataset_fn = functools.partial(
        get_tf_dataset,
        dataset_name=dataset_name,
        subset_name=subset_name,
        template=template,
        split_mapping=split_mapping,
    )
    data_source = seqio.FunctionDataSource(
        dataset_fn,
        splits=list(split_mapping.keys()),
        num_input_examples={s: dataset_splits[split_mapping[s]].num_examples for s in split_mapping.keys()},
    )
    output_features = {
        "inputs": seqio.Feature(t5.data.get_default_vocabulary(), add_eos=False, dtype=tf.int32),
        "targets": seqio.Feature(t5.data.get_default_vocabulary(), add_eos=True, dtype=tf.int32),
    }
    preprocessors = [
        seqio.preprocessors.tokenize,
        seqio.preprocessors.append_eos,
        seqio.CacheDatasetPlaceholder(required=False),
    ]

    # Add train and normal eval tasks
    seqio.TaskRegistry.add(
        task_name,
        data_source,
        preprocessors=preprocessors,
        output_features=output_features,
        metric_fns=metrics,
        postprocess_fn=maybe_get_class_id_postprocessor(template),
    )

    # Add rank classification eval task
    if template.answer_choices:
        rank_classification_preprocessor = functools.partial(
            t5.data.preprocessors.rank_classification,
            inputs_fn=lambda ex: tf.fill((len(ex["answer_choices"]),), ex["inputs"]),
            targets_fn=lambda ex: ex["answer_choices"],
            is_correct_fn=lambda ex: tf.equal(ex["answer_choices"], tf.strings.strip(ex["targets"])),
            weight_fn=lambda ex: 1.0,
        )
        fixed_choices = template.get_fixed_answer_choices_list()
        num_classes = len(fixed_choices) if fixed_choices else None
        seqio.TaskRegistry.add(
            task_name + "_score_eval",
            data_source,
            preprocessors=[rank_classification_preprocessor] + preprocessors,
            output_features=output_features,
            metric_fns=[functools.partial(t5.evaluation.metrics.rank_classification, num_classes=num_classes)],
            postprocess_fn=t5.data.postprocessors.rank_classification,
        )


datatset_subset_tuple = Tuple[str, Optional[str]]
t0_eval: Dict[str, List[datatset_subset_tuple]] = {
    "BASE": [],
    "BIAS_FAIRNESS": []
}
t0_train: Dict[str, List[datatset_subset_tuple]] = {
    "BASE": [],
    # GPT3 evaluation set
    "GPT_EVAL": [],
    # SuperGLUE (except RTE and CB)
    "SGLUE": []
}

gsheet: Dict[datatset_subset_tuple, Dict] = {}
experiment_path = pkg_resources.resource_filename(__name__, "../datasets.csv")
with open(experiment_path) as exp_file:
    reader = csv.DictReader(exp_file)
    for row in reader:
        if row["subset"] == "":
            row["subset"] = None  # to match promptsource.Template object
        dataset_subset = (row["HF_name"], row["subset"])
        if row["do_train"] != "":
            do_train_source = row["do_train"]
            # sanity checks
            if do_train_source == "SGLUE":
                assert dataset_subset[0] == "super_glue"
            t0_train[do_train_source].append(dataset_subset)
        if row["do_eval"] != "":
            do_eval_source = row["do_eval"]
            # sanity checks
            if do_eval_source == "BIAS_FAIRNESS":
                assert row["task_by_convention"] == "bias_and_fairness"
            t0_eval[do_eval_source].append(dataset_subset)
        gsheet[dataset_subset] = row

all_datasets = sum(t0_train.values(), []) + sum(t0_eval.values(), [])

all_templates = templates.TemplateCollection()
all_templates.remove("anli")  # Need to special-case ANLI due to weird split conventions

# 3 stages of training/ablation: D4 -> GPT -> SuperGLUE
t0_train_mixture: Dict[str,List[str]] = {key: [] for key in t0_train}
t0_eval_mixture: Dict[str,List[str]] = {key: [] for key in t0_eval}
mixture_cap: Dict[str, int] = {}
single_original_task: Dict[Tuple[str, str], str] = {}
all_original_tasks: List[str] = []
for dataset_name, subset_name in all_templates.keys:
    if (dataset_name, subset_name) not in all_datasets:
        all_templates.remove(dataset_name, subset_name)
        continue

    dataset = all_templates.get_dataset(dataset_name, subset_name)
    num_templates = len(dataset.all_template_names)
    train_size = gsheet[(dataset_name, subset_name)]["train_size"]
    if train_size == "":
        train_size = 0
    else:
        train_size = int(train_size)
    if train_size > MAX_EXAMPLES_PER_DATASET:
        cap = MAX_EXAMPLES_PER_DATASET // num_templates
    else:
        cap = train_size
    for template_name in dataset.all_template_names:
        add_task(dataset_name, subset_name, template_name)

        template = dataset[template_name]

        task_name = utils.get_task_name(dataset_name, subset_name, template_name)

        if (dataset_name, subset_name) not in single_original_task and template.metadata.original_task:
            single_original_task[(dataset_name, subset_name)] = task_name

        if template.metadata.original_task:
            all_original_tasks.append(task_name)

        # Check that the dataset_subset_tuple is in t0_train
        for key, dataset_subset_tuples in t0_train.items():
            if (dataset_name, subset_name) in dataset_subset_tuples:
                t0_train_mixture[key].append(task_name)
                mixture_cap[task_name] = cap

        # Check that the dataset_subset_tuple is in t0_eval
        if (dataset_name, subset_name) in t0_eval["BASE"]:
            if template.metadata.original_task:
                t0_eval_mixture["BASE"].append(task_name)
            # TODO use template.metadata.answer_choices here for rank eval
        if (dataset_name, subset_name) in t0_eval["BIAS_FAIRNESS"]:
            t0_eval_mixture["BIAS_FAIRNESS"].append(task_name)

# Special case for ANLI, which has weirdly-named splits and rounds that should be subsets
dataset_name, subset_name = ("anli", None)
dataset = all_templates.get_dataset(dataset_name, subset_name)
for anli_round in ("r1", "r2", "r3"):
    for template_name in all_templates.get_dataset(dataset_name, subset_name).all_template_names:
        task_name = utils.get_task_name(dataset_name, subset_name, template_name) + f"_{anli_round}"
        split_mapping = {
            "train": f"train_{anli_round}",
            "validation": f"dev_{anli_round}",
            "test": f"test_{anli_round}",
        }
        add_task(dataset_name, subset_name, template_name, task_name, split_mapping)

        template = dataset[template_name]
        if template.metadata.original_task:
            t0_eval_mixture["BASE"].append(task_name)  # TODO or add to ANLI special mixture
        # TODO use template.metadata.answer_choices here for rank eval


TASK_BLACKLIST = [
    # Tasks which often tokenize to > 1024 tokens currently
    "hotpot_qa_distractor_Generate_Explanations",
    "hotpot_qa_fullwiki_Generate_Explanations",
    "hotpot_qa_distractor_Generate_Answer_and_Explanations",
    "hotpot_qa_fullwiki_Generate_Answer_and_Explanations",
    "hotpot_qa_fullwiki_Generate_Answer",
    "hotpot_qa_distractor_Generate_Answer",
    "hotpot_qa_distractor_Generate_Title_2",
    "hotpot_qa_fullwiki_Generate_Title_2",
    "hotpot_qa_fullwiki_Generate_Title_1",
    "hotpot_qa_distractor_Generate_Title_1",
    "hotpot_qa_distractor_Generate_Question",
    "hotpot_qa_fullwiki_Generate_Question",
    "tab_fact_tab_fact_tab_fact_3",
    "tab_fact_tab_fact_tab_fact_2",
    "tab_fact_tab_fact_tab_fact_1",
    "tab_fact_tab_fact_tab_fact_7",
    "tab_fact_tab_fact_tab_fact_4",
    "tab_fact_tab_fact_tab_fact_5",
    "tab_fact_tab_fact_tab_fact_6",
    "wiki_hop_masked_Choose_Best_Object_Candidate",
    "wiki_hop_masked_Indirect_Question_about_Birthplace_Citizenship_Place_of_Death",
    "narrativeqa_Template_05",
    "ecthr_cases_alleged_violation_prediction_silver_rationales",
    # Tasks with broken cached files
    "gigaword_summarize_",
]

# Tasks that failed caching (won't try to fix them for now) - remove when we are done
D4_TRAIN_SCORE_EVAL_TASK_BLACKLIST = [
    "amazon_polarity_Is_this_product_review_positive_score_eval",
    "amazon_polarity_Is_this_review_negative_score_eval",
    "amazon_polarity_Is_this_review_score_eval",
    "amazon_polarity_User_recommend_this_product_score_eval",
    "amazon_polarity_convey_negative_or_positive_sentiment_score_eval",
    "amazon_polarity_flattering_or_not_score_eval",
    "amazon_polarity_negative_or_positive_tone_score_eval",
    "amazon_polarity_user_satisfied_score_eval",
    "amazon_polarity_would_you_buy_score_eval",
    "dbpedia_14_given_a_choice_of_categories__score_eval",
    "dbpedia_14_given_list_what_category_does_the_paragraph_belong_to_score_eval",
    "dbpedia_14_pick_one_category_for_the_following_text_score_eval",
    "wiki_hop_original_choose_best_object_affirmative_1_score_eval",
    "wiki_hop_original_choose_best_object_affirmative_2_score_eval",
    "wiki_hop_original_choose_best_object_affirmative_3_score_eval",
    "wiki_hop_original_choose_best_object_interrogative_1_score_eval",
    "wiki_hop_original_choose_best_object_interrogative_2_score_eval",
]

seqio.MixtureRegistry.add(
    "t0_train",
    [task for task in t0_train_mixture["BASE"] if task not in TASK_BLACKLIST],
    default_rate=lambda t: mixture_cap[t.name],
)

seqio.MixtureRegistry.add(
    "t0+_train",
    [task for task in t0_train_mixture["BASE"] + t0_train_mixture["GPT_EVAL"] if task not in TASK_BLACKLIST],
    default_rate=lambda t: mixture_cap[t.name],
)

seqio.MixtureRegistry.add(
    "t0++_train",
    [task for task in t0_train_mixture["BASE"] + t0_train_mixture["GPT_EVAL"] + t0_train_mixture["SGLUE"] if task not in TASK_BLACKLIST],
    default_rate=lambda t: mixture_cap[t.name],
)

seqio.MixtureRegistry.add(
    "t0_eval_score_eval",
    [
        task
        for task in seqio.TaskRegistry.names()
        if task.endswith("_score_eval")
        and task.split("_score_eval")[0] in t0_eval_mixture["BASE"]
        and task.split("_score_eval")[0] not in TASK_BLACKLIST
    ],
    default_rate=functools.partial(seqio.mixing_rate_num_examples, maximum=500_000),
)

# Train tasks we don't care about evaluating on
D4_TRAIN_SKIP_EVAL = [
    "paws_labeled_final",
    "adversarial_qa_dbidaf",
    "adversarial_qa_dbert",
    "duorc_ParaphraseRC",
    "dream",
    "amazon_polarity",
    "app_reviews",
    "imdb",
    "wiki_bio",
    "gigaword",
    "multi_news",
    "samsum",
    "dbpedia_14",
    "trec",
]

seqio.MixtureRegistry.add(
    "t0_train_score_eval",
    [
        task
        for task in seqio.TaskRegistry.names()
        if task.endswith("_score_eval")
        and task.split("_score_eval")[0] in t0_train_mixture["BASE"]
        and task.split("_score_eval")[0] not in TASK_BLACKLIST
        and task not in D4_TRAIN_SCORE_EVAL_TASK_BLACKLIST
        and not any([skip in task for skip in D4_TRAIN_SKIP_EVAL])
        and task.split("_score_eval")[0] in all_original_tasks
    ],
    default_rate=functools.partial(seqio.mixing_rate_num_examples, maximum=500_000),
)

seqio.MixtureRegistry.add(
    "t0_train_one_og_prompt",
    [task for task in single_original_task.values() if task in t0_train_mixture["BASE"] and task not in TASK_BLACKLIST],
    default_rate=lambda t: mixture_cap[t.name],
)

seqio.MixtureRegistry.add(
    "t0_train_all_og_prompts",
    [task for task in all_original_tasks if task in t0_train_mixture["BASE"] and task not in TASK_BLACKLIST],
    default_rate=lambda t: mixture_cap[t.name],
)

seqio.MixtureRegistry.add(
    "bias_fairness_eval_score_eval",
    [
        task
        for task in seqio.TaskRegistry.names()
        if task.endswith("_score_eval") and task.split("_score_eval")[0] in t0_eval_mixture["BIAS_FAIRNESS"]
    ],
    default_rate=functools.partial(seqio.mixing_rate_num_examples, maximum=500_000),
)
