# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import random
from collections import defaultdict

from flask import request
from flask_restx import Resource, reqparse

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde.apis.models.kobold_v2 import TextModels, TextParsers
from horde.apis.v2.base import (
    GenerateTemplate,
    JobPopTemplate,
    JobSubmitTemplate,
    api,
)
from horde.classes.base import settings
from horde.classes.base.style import StyleCollection
from horde.classes.kobold.genstats import (
    get_compiled_textgen_stats_models,
    get_compiled_textgen_stats_totals,
)
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.database import functions as database
from horde.database import text_functions as text_database
from horde.flask import cache, db
from horde.limiter import limiter
from horde.logger import logger
from horde.model_reference import model_reference
from horde.utils import hash_dictionary
from horde.validation import ParamValidator
from horde.vars import horde_title

models = TextModels(api)
parsers = TextParsers()


class TextAsyncGenerate(GenerateTemplate):
    gentype = "text"
    decorators = [
        limiter.limit(
            limit_value=lim.get_request_90min_limit_per_ip,
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
        limiter.limit(
            limit_value=lim.get_request_limit_per_apikey,
            key_func=lim.get_request_api_key,
        ),
    ]

    @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
    @api.marshal_with(
        models.response_model_async,
        code=202,
        description="Generation Queued",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(503, "Maintenance Mode", models.response_model_error)
    @api.response(429, "Too Many Prompts", models.response_model_error)
    def post(self):
        """Initiate an Asynchronous request to generate text.
        This endpoint will immediately return with the UUID of the request for generation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request.
        Perhaps some will appear in the next 20 minutes.
        Asynchronous requests live for 20 minutes before being considered stale and being deleted.
        """
        self.args = parsers.generate_parser.parse_args()
        try:
            super().post()
        except KeyError as e:
            logger.error("caught missing Key.")
            logger.error(self.args)
            logger.error(self.args.params)
            raise e
            return {"message": "Internal Server Error"}, 500
        if self.args.dry_run:
            ret_dict = {"kudos": round(self.kudos)}
            return ret_dict, 200
        ret_dict = {
            "id": self.wp.id,
            "kudos": round(self.kudos),
        }
        if not database.wp_has_valid_workers(self.wp) and not settings.mode_raid():
            ret_dict["message"] = self.get_size_too_big_message()
        return (ret_dict, 202)

    def initiate_waiting_prompt(self):
        self.wp = TextWaitingPrompt(
            worker_ids=self.workers,
            models=self.models,
            prompt=self.prompt,
            user_id=self.user.id,
            params=self.params,
            softprompt=self.args.softprompt,
            trusted_workers=self.args.trusted_workers,
            validated_backends=self.args.validated_backends,
            worker_blacklist=self.args.worker_blacklist,
            slow_workers=self.args.slow_workers,
            ipaddr=self.user_ip,
            safe_ip=True,
            client_agent=self.args["Client-Agent"],
            sharedkey_id=self.sharedkey.id if self.sharedkey else None,
            proxied_account=self.args["proxied_account"],
            webhook=self.args.webhook,
        )
        _, total_threads = database.count_active_workers("text")
        highest_multiplier = 0
        if len(self.models) == 0:
            required_kudos = 20 * self.wp.n
        else:
            # We find the highest multiplier to avoid someone gaming the system by requesting
            # a small model along with a big model.
            for model in self.models:
                model_multiplier = model_reference.get_text_model_multiplier(model)
                if model_multiplier > highest_multiplier:
                    highest_multiplier = model_multiplier
            required_kudos = round(self.wp.max_length * highest_multiplier / 21, 2) * self.wp.n
        needs_kudos, tokens, disable_downgrade = self.wp.require_upfront_kudos(database.retrieve_totals(), total_threads)
        if self.sharedkey and self.sharedkey.kudos != -1 and required_kudos > self.sharedkey.kudos:
            if self.args.allow_downgrade and not disable_downgrade:
                self.downgrade_wp_priority = True
            else:
                self.wp.delete()
                raise e.KudosUpfront(
                    required_kudos,
                    self.username,
                    message=f"This shared key does not have enough remaining kudos ({self.sharedkey.kudos}) "
                    f"to fulfill this reques ({required_kudos}).",
                    rc="SharedKeyInsufficientKudos",
                )
        if needs_kudos:
            if required_kudos > self.user.kudos:
                if self.args.allow_downgrade and not disable_downgrade:
                    self.wp.downgrade(tokens)
                else:
                    self.wp.delete()
                    raise e.KudosUpfront(
                        required_kudos,
                        self.username,
                        message=f"Due to heavy demand, for requests over {tokens} tokens, "
                        "the client needs to already have the required kudos. "
                        f"This request requires {required_kudos} kudos to fulfil.",
                    )

        if self.sharedkey:
            is_in_limit, fail_message = self.sharedkey.is_job_within_limits(
                text_tokens=self.wp.max_length,
            )
            if not is_in_limit:
                # If we are using the shared key assigned to a style, then we bypass the shared key requirements
                # since its owner explicitly allowed to be used with a style exceeding them
                if not (self.existing_style and self.existing_style.sharedkey and self.existing_style.sharedkey.id == self.sharedkey.id):
                    self.wp.delete()
                    raise e.BadRequest(fail_message)

    def get_size_too_big_message(self):
        return (
            "Warning: No available workers can fulfill this request. It will expire in 20 minutes. "
            "Consider reducing the amount of tokens to generate."
        )

    def validate(self):
        self.prompt = self.args.prompt
        self.apikey = self.args.apikey
        self.apply_style()
        super().validate()
        param_validator = ParamValidator(self.prompt, self.args.models, self.params, self.user)
        self.warnings = param_validator.validate_text_params()
        if self.args.extra_source_images is not None and len(self.args.extra_source_images) > 0:
            raise e.BadRequest("This request type does not accept extra source images.", rc="InvalidExtraSourceImages.")

    def get_hashed_params_dict(self):
        gen_payload = self.params.copy()
        ## IMPORTANT: When adjusting this, also adjust TextWaitingPrompt.calculate_kudos()
        # We need to also use the model list into our hash, as our kudos calculation is based on whichever model is first
        gen_payload["models"] = self.args.models
        params_hash = hash_dictionary(gen_payload)
        # logger.debug([params_hash,gen_payload])
        return params_hash

    def apply_style(self):
        if self.args.style is None:
            return
        # The super() ensures the common parts of applying a style
        super().apply_style()
        if self.existing_style.style_type != "text":
            raise e.BadRequest("Image styles cannot be used on image requests", "StyleMismatch")
        if isinstance(self.existing_style, StyleCollection):
            colstyles = self.existing_style.styles
            random.shuffle(colstyles)
            self.existing_style.use_count += 1
            self.existing_style = colstyles[0]
        self.models = self.existing_style.get_model_names()
        # We need to use defaultdict to avoid getting keyerrors in case the style author added
        # Erroneous keys in the string
        self.prompt = self.existing_style.prompt.format_map(defaultdict(str, p=self.prompt))
        requested_n = self.params.get("n", 1)
        self.params = copy.deepcopy(self.existing_style.params)
        self.params["n"] = requested_n
        self.nsfw = self.existing_style.nsfw
        self.existing_style.use_count += 1
        if self.existing_style.user != self.user:
            self.existing_style.user.record_style(2, "text")
            self.style_kudos = True
        db.session.commit()
        logger.debug(f"Style '{self.args.style}' applied.")


class TextAsyncStatus(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    # If I marshal it here, it overrides the marshalling of the child class unfortunately
    decorators = [limiter.limit("60/minute", key_func=lim.get_request_path)]

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_wp_status_full,
        code=200,
        description="Async Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def get(self, id=""):
        """Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated texts.
        """
        self.args = self.get_parser.parse_args()
        wp = text_database.get_text_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(
                id,
                request_type="Text Waiting Prompt (Status)",
                client_agent=self.args["Client-Agent"],
                ipaddr=request.remote_addr,
            )
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text"),
        )
        return (wp_status, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @api.expect(delete_parser)
    @api.marshal_with(
        models.response_model_wp_status_full,
        code=200,
        description="Async Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def delete(self, id=""):
        """Cancel an unfinished request.
        This request will include all already generated texts.
        """
        self.args = self.delete_parser.parse_args()
        wp = text_database.get_text_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(
                id,
                request_type="Text Waiting Prompt (Delete)",
                client_agent=self.args["Client-Agent"],
                ipaddr=request.remote_addr,
            )
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text"),
        )
        logger.info(f"Request with ID {wp.id} has been cancelled.")
        # FIXME: I pevent it at the moment due to the race conditions
        # The WPCleaner is going to clean it up anyway
        wp.n = 0
        db.session.commit()
        return (wp_status, 200)


class TextJobPop(JobPopTemplate):
    worker_class = TextWorker
    decorators = [limiter.limit("60/second")]

    @api.expect(parsers.job_pop_parser, models.input_model_job_pop, validate=True)
    @api.marshal_with(models.response_model_job_pop, code=200, description="Generation Popped")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def post(self):
        """Check if there are generation requests queued for fulfillment.
        This endpoint is used by registered workers only
        """
        # Splitting the post to its own function so that I can have the decorators of post on each extended class
        # Without copying the whole post() code
        self.args = parsers.job_pop_parser.parse_args()
        return super().post()

    def check_in(self):
        self.softprompts = []
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        models = self.models
        self.worker.check_in(
            self.args["max_length"],
            self.args["max_context_length"],
            self.softprompts,
            models=models,
            nsfw=self.args.nsfw,
            safe_ip=self.safe_ip,
            ipaddr=self.worker_ip,
            threads=self.args.threads,
            bridge_agent=self.args.bridge_agent,
        )

    def get_sorted_wp(self, priority_user_ids=None):
        """We're sending the lists directly, to avoid having to join tables"""
        sorted_wps = text_database.get_sorted_text_wp_filtered_to_worker(
            self.worker,
            self.models,
            priority_user_ids=priority_user_ids,
            page=self.wp_page,
        )
        return sorted_wps


class TextJobSubmit(JobSubmitTemplate):
    decorators = [limiter.limit("60/second")]

    @api.expect(parsers.job_submit_parser, models.input_model_job_submit, validate=True)
    @api.marshal_with(models.response_model_job_submit, code=200, description="Generation Submitted")
    @api.response(400, "Generation Already Submitted", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Request Not Found", models.response_model_error)
    def post(self):
        """Submit generated text.
        This endpoint is used by registered workers only
        """
        # We have to parse the args here, to ensure we use the correct parser class
        self.args = parsers.job_submit_parser.parse_args()
        return super().post()

    def get_progen(self):
        """Set to its own function to it can be overwritten depending on the class"""
        return text_database.get_text_progen_by_id(self.args["id"])


class TextHordeStatsTotals(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_stats_img_totals,
        code=200,
        description=f"{horde_title} generated text statistics",
    )
    def get(self):
        """Details how many texts have been generated in the past minux,hour,day,month and total
        Also shows the amount of pixelsteps for the same timeframe.
        """
        return get_compiled_textgen_stats_totals(), 200


class TextHordeStatsModels(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_stats_models,
        code=200,
        description=f"{horde_title} generated text statistics per model",
    )
    def get(self):
        """Details how many texts were generated per model for the past day, month and total"""
        return get_compiled_textgen_stats_models(), 200


class KoboldKudosTransfer(Resource):
    post_parser = reqparse.RequestParser()
    post_parser.add_argument("kai_id", type=int, required=True, location="json")
    post_parser.add_argument("kudos_amount", type=int, required=True, location="json")
    post_parser.add_argument("trusted", type=bool, default=False, required=True, location="json")

    @api.expect(post_parser)
    def post(self, user_id=""):
        """Receives kudos from the KoboldAI Horde"""
        if request.remote_addr != "167.86.124.45":
            raise e.BadRequest("Access Denied")
        user = database.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        self.args = self.post_parser.parse_args()
        logger.warning(
            f"{user.get_unique_alias()} Started {self.args.kudos_amount}Kudos Transfer from KAI ID {self.args.kai_id}",
        )
        if user.trusted is False and self.args.trusted is True:
            user.set_trusted(self.args.trusted)
        user.modify_kudos(self.args.kudos_amount, "koboldai")
        return {"new_kudos": user.kudos}, 200
