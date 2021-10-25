import os
import logging
import random
import re

import sentry_sdk

from collections import defaultdict
import spacy
import json

import nltk
from nltk import wordpunct_tokenize
from nltk import word_tokenize
import dff.transitions as trn
from dff.core.keywords import TRANSITIONS, RESPONSE
from dff.core import Context, Actor

import sf_utils

sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"))

logger = logging.getLogger(__name__)

# required for Generic Response function
nlp = spacy.load("en_core_web_sm")
nltk.download("punkt")


registers = ['God', 'Gosh', 'Hm', 'Hmm', 'Hunh', 'Mhm', 'Mm', 'Oh', 'Okay', 'Unhunh', 'Well', 'Yeah', 'Yes', 'whoa', 'yeah']

# endregion


# region CONFIDENCES
DIALOG_BEGINNING_START_CONFIDENCE = 0.98
DIALOG_BEGINNING_CONTINUE_CONFIDENCE = 0.9
DIALOG_BEGINNING_SHORT_ANSWER_CONFIDENCE = 0.98
MIDDLE_DIALOG_START_CONFIDENCE = 0.7
SUPER_CONFIDENCE = 1.0
HIGH_CONFIDENCE = 0.98

MUST_CONTINUE_CONFIDENCE = 0.98
CAN_CONTINUE_CONFIDENCE = 0.9
CANNOT_CONTINUE_CONFIDENCE = 0.0


patterns_supported_speech_functions = ["Register", "Check", "Confirm", "Monitor", "Affirm", "Agree", "Clarify"]

supported_speech_functions_patterns_re = re.compile(
    "(" + "|".join(patterns_supported_speech_functions) + ")", re.IGNORECASE
)


def is_supported_speech_function(ctx, human_utterance, bot_utterance):
    sf_functions = sf_utils.get_speech_function(ctx)
    logger.info(f"Found Speech Function(s): {sf_functions}")

    sf_predictions = sf_utils.get_speech_function_predictions(ctx)
    if sf_predictions:
        sf_predictions_list = list(sf_predictions)
        sf_predictions_for_last_phrase = sf_predictions_list[-1]

        for sf_predicted in sf_predictions_for_last_phrase:
            prediction = sf_predicted["prediction"]
            logger.info(f"prediction: {prediction}")
            supported = bool(re.search(supported_speech_functions_patterns_re, prediction))
            if supported:
                logger.info(
                    f"At least one of the proposed speech functions is supported "
                    f"for generic response: {sf_predicted}"
                )
                return True
    return True


def get_pre_last_human_utterance(ctx):
    return list(ctx.requests.values())[-2]


def get_pre_last_bot_utterance(ctx):
    return list(ctx.responses.values())[-2]


def is_last_bot_utterance_by_us(ctx):
    bot_utterances = list(ctx.responses.values())
    if len(bot_utterances) == 0:
        return False

    last_bot_utterance = ctx.last_response

    active_skill = last_bot_utterance["active_skill"]

    if active_skill == "dff_generic_responses_skill":
        return True

    return False


reply_affirm = [
    "Oh definitely.", "Yeah.", "Kind of.", "Unhunh",
    "Yeah I think so", "Really.", "Right.", "That's what it was."
]


def clarify_response(previous_phrase):
    doc = nlp(previous_phrase)
    poses = []
    deps = []
    for token in doc:
        poses.append(token.pos_)
        deps.append(token.dep_)
        if token.pos_ == "NOUN" or token.pos_ == "PROPN":
            clarify_noun = token.text
            next_sent = "What " + clarify_noun + "?"
        elif token.dep_ == "prep":
            prep = token.text
            next_sent = str(prep).capitalize() + " what?"
        elif poses[0] == "PROPN" or poses[0] == "PRON":
            if word_tokenize(previous_phrase)[0].lower() == "i" or word_tokenize(previous_phrase)[0].lower() == "we":
                first_pron = "You"
                next_sent = first_pron + " what?"
            else:
                if word_tokenize(previous_phrase)[0].lower() != "what":
                    next_sent = word_tokenize(previous_phrase)[0] + " what?"
                else:
                    next_sent = "What?"
        else:
            next_sent = "What?"
    return next_sent


def confirm_response(previous_phrase):
    track_confirm = ["Oh really?", " Oh yeah?", "Sure?", "Are you sure?", "Are you serious?", "Yeah?"]
    if len(word_tokenize(previous_phrase)) > 5:
        next_sent = (word_tokenize(previous_phrase))[-1].capitalize() + "?"
    elif len(word_tokenize(previous_phrase)) < 4:
        if "you" in word_tokenize(previous_phrase):
            previous_phrase = re.sub("you", "me", previous_phrase)
        if "I " in previous_phrase:
            previous_phrase = re.sub("I", "you", previous_phrase)
        next_sent = previous_phrase + "?"
    else:
        next_sent = random.choice(track_confirm)
    return next_sent


def generate_response(ctx, predicted_sf, previous_phrase, enable_repeats_register=False, user_name=""):
    response = None
    if "Register" in predicted_sf:
        response = random.choice(registers)
        if enable_repeats_register is True:
            response = word_tokenize(previous_phrase)[-1].capitalize() + "."
    if "Check" in predicted_sf:
        response = sf_utils.get_not_used_and_save_generic_response(predicted_sf, ctx)
    if "Confirm" in predicted_sf:
        response = confirm_response(previous_phrase)
    if "Affirm" in predicted_sf:
        response = sf_utils.get_not_used_and_save_generic_response(predicted_sf, ctx)
    if "Agree" in predicted_sf:
        response = sf_utils.get_not_used_and_save_generic_response(predicted_sf, ctx)
    if "Clarify" in predicted_sf:
        response = clarify_response(previous_phrase)
    return response


##################################################################################################################
# Handlers
##################################################################################################################


# region RESPONSE_TO_SPEECH_FUNCTION
##################################################################################################################


def generic_response_condition(ctx: Context, actor: Actor, *args, **kwargs):
    flag = False
    try:
        flag = is_supported_speech_function(ctx, human_utterance, bot_utterance)

    except Exception as exc:
        logger.exception(exc)
        logger.info(f"sys_response_to_speech_function_request: Exception: {exc}")
        sentry_sdk.capture_exception(exc)

    logger.info(f"sys_response_to_speech_function_request: {flag}")
    return flag


def generic_response_generate(ctx: Context, actor: Actor, *args, **kwargs):
    logger.debug("exec usr_response_to_speech_function_response")
    interrogative_words = ["whose", "what", "which", "who", "whom", "what", "which", "why", "where", "when", "how"]
    
    try:
        human_utterance = ctx.last_request
        phrases = nltk.sent_tokenize(human_utterance)

        sf_functions = None
        cont = False

        if is_last_bot_utterance_by_us(ctx) or len(word_tokenize(human_utterance["text"])) > 10:
            # check for "?" symbol in the standalone segments of the original user's utterance
            for phrase in phrases:
                if "?" not in phrase:
                    cont = True
                else:
                    cont = False
            if cont:
                sf_functions = sf_utils.get_speech_function_for_human_utterance(ctx)
                logger.info(f"Found Speech Function: {sf_functions}")
            else:
                if word_tokenize(human_utterance["text"])[0] not in interrogative_words:
                    sf_functions = sf_utils.get_speech_function_for_human_utterance(ctx)
                    logger.info(f"Found Speech Function: {sf_functions}")

        if not sf_functions:
            return ""

        last_phrase_function = list(sf_functions)[-1]

        sf_predictions = sf_utils.get_speech_function_predictions(ctx)
        logger.info(f"Proposed Speech Functions: {sf_predictions}")

        if not sf_predictions:
            return ""

        generic_responses = []

        sf_predictions_list = list(sf_predictions)
        sf_predictions_for_last_phrase = sf_predictions_list[-1]

        for sf_prediction in sf_predictions_for_last_phrase:
            prediction = sf_prediction["prediction"]
            generic_response = generate_response(ctx, prediction, last_phrase_function, False, "")
            if generic_response is not None:
                if generic_response != "??" and generic_response != ".?":
                    generic_responses.append(generic_response)

        # generating response
        questions = ["Hi", "Hello", "Well hello there!", "Look what the cat dragged in!"]  

        if not generic_responses:
            response = random.choice(questions)

        # actual generic response
        if generic_responses:
            response = random.choice(generic_responses)
        
        return response
    
    except Exception as exc:
        logger.exception(exc)
        logger.info(f"usr_response_to_speech_function_response: Exception: {exc}")
        sentry_sdk.capture_exception(exc)
        return ""


generic_responses_flow = {
    "start_node": {
        RESPONSE: "",
        TRANSITIONS: {"generic_response": generic_response_condition},
    },
    "generic_response": {
        RESPONSE: generic_response_generate,
        TRANSITIONS: {trn.repeat(): generic_response_condition},
    }
}
