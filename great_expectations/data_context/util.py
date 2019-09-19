import requests
import datetime
import logging
import os
import json
import errno
import six
import importlib
import copy
import re

logger = logging.getLogger(__name__)


def build_slack_notification_request(validation_json=None):
    # Defaults
    timestamp = datetime.datetime.strftime(datetime.datetime.now(), "%x %X")
    status = "Failed :x:"
    run_id = None

    title_block = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "No validation occurred. Please ensure you passed a validation_json.",
        },
    }

    query = {"blocks": [title_block]}

    if validation_json:
        if "meta" in validation_json:
            data_asset_name = validation_json["meta"].get(
                "data_asset_name", "no_name_provided_" + datetime.datetime.utcnow().isoformat().replace(":", "") + "Z"
            )
            expectation_suite_name = validation_json["meta"].get("expectation_suite_name", "default")

        n_checks_succeeded = validation_json["statistics"]["successful_expectations"]
        n_checks = validation_json["statistics"]["evaluated_expectations"]
        run_id = validation_json["meta"].get("run_id", None)
        check_details_text = "{} of {} expectations were met\n\n".format(
            n_checks_succeeded, n_checks)

        if validation_json["success"]:
            status = "Success :tada:"

        query["blocks"][0]["text"]["text"] = "*Validated batch from data asset:* `{}`\n*Status: {}*\n{}".format(
            data_asset_name, status, check_details_text)
        if "batch_kwargs" in validation_json["meta"]:
            batch_kwargs = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Batch kwargs: {}".format(
                json.dumps(validation_json["meta"]["batch_kwargs"], indent=2))
                }
            }
            query["blocks"].append(batch_kwargs)

        if "result_reference" in validation_json["meta"]:
            report_element = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "- *Validation Report*: {}".format(validation_json["meta"]["result_reference"])},
            }
            query["blocks"].append(report_element)

        if "dataset_reference" in validation_json["meta"]:
            dataset_element = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "- *Validation data asset*: {}".format(validation_json["meta"]["dataset_reference"])
                },
            }
            query["blocks"].append(dataset_element)

    footer_section = {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "Great Expectations run id {} ran at {}".format(run_id, timestamp),
            }
        ],
    }
    query["blocks"].append(footer_section)
    return query


def get_slack_callback(webhook):
    def send_slack_notification(validation_json=None):
        """Post a slack notification."""
        session = requests.Session()
        query = build_slack_notification_request(validation_json)

        try:
            response = session.post(url=webhook, json=query)
        except requests.ConnectionError:
            logger.warning(
                'Failed to connect to Slack webhook at {url} '
                'after {max_retries} retries.'.format(
                    url=webhook, max_retries=10))
        except Exception as e:
            logger.error(str(e))
        else:
            if response.status_code != 200:
                logger.warning(
                    'Request to Slack webhook at {url} '
                    'returned error {status_code}: {text}'.format(
                        url=webhook,
                        status_code=response.status_code,
                        text=response.text))
    return send_slack_notification


def safe_mmkdir(directory, exist_ok=True):
    """Simple wrapper since exist_ok is not available in python 2"""
    if not isinstance(directory, six.string_types):
        raise TypeError("directory must be of type str, not {0}".format({
            "directory_type": str(type(directory))
        }))

    if not exist_ok:
        raise ValueError(
            "This wrapper should only be used for exist_ok=True; it is designed to make porting easier later")
    try:
        os.makedirs(directory)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

# TODO : Consider moving this into types.resource_identifiers.DataContextKey.
# NOTE : We **don't** want to encourage stringification of keys, other than in tests, etc.
# TODO : Rename to parse_string_to_data_context_key
def parse_string_to_data_context_resource_identifier(string, separator="."):
    string_elements = string.split(separator)

    loaded_module = importlib.import_module("great_expectations.data_context.types.resource_identifiers")
    class_ = getattr(loaded_module, string_elements[0])

    class_instance = class_(*(string_elements[1:]))

    return class_instance

def load_class(class_name, module_name):
    # Get the class object itself from strings.
    loaded_module = importlib.import_module(module_name)
    try:
        class_ = getattr(loaded_module, class_name)
    except AttributeError as e:
        raise AttributeError("Module : {} has no class named : {}".format(
            module_name,
            class_name,
        ))
    return class_


# TODO: Rename runtime_config to runtime_environment and pass it through as a typed object, rather than unpacking it.
# TODO: Rename config to constructor_kwargs and config_defaults -> constructor_kwarg_default
# TODO: Improve error messages in this method. Since so much of our workflow is config-driven, this will be a *super* important part of DX.
def instantiate_class_from_config(config, runtime_config, config_defaults=None):
    """Build a GE class from configuration dictionaries."""

    if config_defaults is None:
        config_defaults = {}

    config = copy.deepcopy(config)

    module_name = config.pop("module_name", None)
    if module_name is None:
        try:
            module_name = config_defaults.pop("module_name")
        except KeyError as e:
            raise KeyError("Neither config : {} nor config_defaults : {} contains a module_name key.".format(
                config, config_defaults,
            ))
    else:
        # Pop the value without using it, to avoid sending an unwanted value to the config_class
        config_defaults.pop("module_name", None)

    class_name = config.pop("class_name", None)
    if class_name is None:
    # TODO : Trap this error and throw an informative message
        try:
            class_name = config_defaults.pop("class_name")
        except KeyError as e:
            raise KeyError("Neither config : {} nor config_defaults : {} contains a class_name key.".format(
                config, config_defaults,
            ))
    else:
        # Pop the value without using it, to avoid sending an unwanted value to the config_class
        config_defaults.pop("class_name", None)

    class_ = load_class(class_name=class_name, module_name=module_name)

    config_with_defaults = copy.deepcopy(config_defaults)
    config_with_defaults.update(config)
    config_with_defaults.update(runtime_config)

    try:
        class_instance = class_(**config_with_defaults)
    except TypeError as e:
        raise TypeError("Couldn't instantiate class : {} with config : \n\t{}\n \n".format(
            class_name,
            format_dict_for_error_message(config_with_defaults)
        ) + str(e))

    return class_instance

def format_dict_for_error_message(dict_):
    # TODO : Tidy this up a bit. Indentation isn't fully consistent.

    return '\n\t'.join('\t\t'.join((str(key), str(dict_[key]))) for key in dict_)


def substitute_config_variable(template_str, config_variables_dict):
    """
    This method takes a string, and if it is of the form ${SOME_VARIABLE} or $SOME_VARIABLE,
    returns the value of SOME_VARIABLE, otherwise returns the string unchanged.

    If the environment variable SOME_VARIABLE is set, the method uses its value for substitution.
    If it is not set, the value of SOME_VARIABLE is looked up in the config variables store (file).
    If it is not found there, the input string is returned as is.

    :param template_str: a string that might or might not be of the form ${SOME_VARIABLE}
            or $SOME_VARIABLE
    :param config_variables_dict: a dictionary of config variables. It is loaded from the
            config variables store (by default, "uncommitted/config_variables.yml file)
    :return:
    """
    if template_str is None:
        return template_str
    try:
        match = re.search(r'^\$\{(.*?)\}$', template_str) or re.search(r'^\$([_a-z][_a-z0-9]*)$', template_str)
    except TypeError:
        # If the value is not a string (e.g., a boolean), we should return it as is
        return template_str

    if match:
        config_variable_value = os.getenv(match.group(1))
        if not config_variable_value:
            config_variable_value = config_variables_dict.get(match.group(1))
            if not config_variable_value:
                config_variable_value = template_str
    else:
        config_variable_value = template_str

    return config_variable_value




def substitute_all_config_variables(data, replace_variables_dict):
    """
    Substitute all config variables of the form ${SOME_VARIABLE} in a dictionary-like
    config object for their values.

    The method traverses the dictionary recursively.

    :param data:
    :param replace_variables_dict:
    :return: a dictionary with all the variables replaced with their values
    """
    if isinstance(data, dict):
        return {k : substitute_all_config_variables(v, replace_variables_dict) for k, v in data.items()}
    return substitute_config_variable(data, replace_variables_dict)