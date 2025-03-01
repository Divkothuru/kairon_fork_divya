import json
from typing import Text

from loguru import logger
from pymongo.errors import ServerSelectionTimeoutError
from rasa.core.channels import UserMessage
from rasa.core.tracker_store import TrackerStore

from .agent_processor import AgentProcessor
from .. import Utility
from ..live_agent.factory import LiveAgentFactory
from ..shared.actions.utils import ActionUtility
from ..shared.live_agent.processor import LiveAgentsProcessor
from ..shared.metering.constants import MetricType
from ..shared.metering.metering_processor import MeteringProcessor


class ChatUtils:

    @staticmethod
    async def chat(data: Text, account: int, bot: Text, user: Text, is_integration_user: bool = False):
        model = AgentProcessor.get_agent(bot)
        msg = UserMessage(data, sender_id=user, metadata={"is_integration_user": is_integration_user, "bot": bot,
                                                          "account": account, "channel_type": "chat_client"})
        chat_response = await model.handle_message(msg)
        ChatUtils.__attach_agent_handoff_metadata(account, bot, user, chat_response, model.tracker_store)
        return chat_response

    @staticmethod
    def reload(bot: Text):
        AgentProcessor.reload(bot)

    @staticmethod
    def __attach_agent_handoff_metadata(account: int, bot: Text, sender_id: Text, bot_predictions, tracker):
        metadata = {'initiate': False, 'type': None, "additional_properties": None}
        exception = None
        should_initiate_handoff = False
        try:
            config = LiveAgentsProcessor.get_config(bot, mask_characters=False, raise_error=False)
            if config:
                metadata["type"] = config["agent_type"]
                should_initiate_handoff = ChatUtils.__should_initiate_handoff(bot_predictions, config)
                if should_initiate_handoff:
                    metadata["initiate"] = True
                    live_agent = LiveAgentFactory.get_agent(config["agent_type"], config["config"])
                    metadata["additional_properties"] = live_agent.initiate_handoff(bot, sender_id)
                    message_trail = ChatUtils.__retrieve_conversation(tracker, sender_id)
                    live_agent.send_conversation_log(message_trail, metadata["additional_properties"]["destination"])
        except Exception as e:
            logger.exception(e)
            exception = str(e)
            metadata['initiate'] = False
        finally:
            if not Utility.check_empty_string(exception) or should_initiate_handoff:
                MeteringProcessor.add_metrics(
                    bot, account, MetricType.agent_handoff, sender_id=sender_id,
                    agent_type=metadata.get("type"), bot_predictions=bot_predictions, exception=exception
                )

        bot_predictions["agent_handoff"] = metadata
        return metadata

    @staticmethod
    def __retrieve_conversation(tracker, sender_id: Text):
        events = TrackerStore.serialise_tracker(tracker.retrieve(sender_id))
        events = json.loads(events)
        _, message_trail = ActionUtility.prepare_message_trail(events.get("events"))
        return message_trail

    @staticmethod
    def __should_initiate_handoff(bot_predictions, agent_handoff_config):
        predicted_intent = bot_predictions["nlu"]["intent"]["name"]
        predicted_action = bot_predictions["action"]
        trigger_on_intent = predicted_intent in set(agent_handoff_config.get("trigger_on_intents", []))
        trigger_on_action = len(set(predicted_action).intersection(set(agent_handoff_config.get("trigger_on_actions", [])))) > 0
        return agent_handoff_config["override_bot"] or trigger_on_intent or trigger_on_action

    @staticmethod
    def get_last_session_conversation(bot: Text, sender_id: Text):

        """
        List conversation events in last session.

        :param bot: bot id
        :param sender_id: user id
        :return: list of conversation events
        """
        host = Utility.environment['database']['url']
        db = Utility.environment['database']['test_db']
        events = []
        message = None
        client = Utility.create_mongo_client(host)
        with client as client:
            try:
                db = client.get_database(db)
                conversations = db.get_collection(bot)
                events = list(conversations.aggregate([
                    {"$match": {"sender_id": sender_id}},
                    {"$unwind": {"path": "$events", "includeArrayIndex": "arrayIndex"}},
                    {"$match": {"events.event": {"$in": ["session_started", "user", "bot"]}}},
                    {"$project": {"sender_id": 1, "events.event": 1, "events.timestamp": 1, "events.text": 1,
                                  "events.data": 1}},
                    {"$group": {"_id": "$sender_id", "events": {"$push": "$events"},
                                "all_events": {"$push": "$events"}}},
                    {"$unwind": {"path": "$events", "includeArrayIndex": "arrayIndex"}},
                    {"$match": {"events.event": "session_started"}},
                    {"$group": {"_id": "$sender_id", "events": {"$push": "$arrayIndex"},
                                "allevents": {"$first": '$all_events'}}},
                    {"$project": {"lastindex": {"$last": "$events"}, "allevents": 1}},
                    {"$project": {"events": {"$slice": ["$allevents", "$lastindex", {"$size": '$allevents'}]}}},
                ]))
                if events:
                    events = events[0]['events']
            except ServerSelectionTimeoutError as e:
                logger.error(e)
                message = f'Failed to retrieve conversation: {e}'
            except Exception as e:
                logger.error(e)
                message = f'Failed to retrieve conversation: {e}'
            return events, message
