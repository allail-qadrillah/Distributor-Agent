from dataclasses import dataclass

from app.agents.orchestrator import AgentOrchestrator
from app.agents.providers.surpluss import SurplussProvider
from app.config import Settings
from app.config import settings as default_settings
from app.intent.classifier import MessageIntentClassifier
from app.intent.gate import IntentGate
from app.messaging.approval import ApprovalManager
from app.messaging.conversation_service import ConversationService
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from app.messaging.locking import operator_lock
from app.messaging.outbox import OutboxProcessor
from app.messaging.processor import MessageProcessor
from app.repositories.approval import ApprovalRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.customer import CustomerRepository
from app.repositories.inventory import InventoryRepository
from app.repositories.message import MessageRepository
from app.repositories.order import OrderRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.price import PriceRepository
from app.repositories.product import ProductRepository
from app.services.order import OrderService
from app.services.product_inquiry import ProductInquiryService
from app.tools.executor import build_tool_executor
from app.tools.handlers import ToolServices
from app.whatsapp.adapter import WhatsAppAdapter


@dataclass(frozen=True)
class MessagingStack:
    processor: MessageProcessor
    conversations: ConversationService
    handoff: HandoffManager
    outbox: OutboxProcessor


def build_messaging_stack(settings: Settings | None = None) -> MessagingStack:
    s = settings if settings is not None else default_settings

    conversation_repository = ConversationRepository()
    message_repository = MessageRepository()
    outbox_repository = OutboxRepository()
    approval_repository = ApprovalRepository()
    customer_repository = CustomerRepository()
    product_repository = ProductRepository()
    price_repository = PriceRepository()
    inventory_repository = InventoryRepository()
    order_repository = OrderRepository()

    identity = IdentityResolver(customer_repository)
    conversations = ConversationService(
        conversation_repository,
        message_repository,
        outbox_repository,
        identity,
        approval_repository,
    )
    # operator transitions wait BOUNDED for the phone lock (an agent run
    # can hold it for up to the provider's 120s read timeout; the route
    # then answers 503 instead of hanging). The processor never uses this
    # factory: it already holds the lock and calls with acquire_lock=False.
    handoff = HandoffManager(conversations, lock_factory=operator_lock)
    approvals = ApprovalManager(approval_repository)

    classifier = (
        MessageIntentClassifier(
            api_key=s.intent_classifier_api_key,
            model=s.intent_classifier_model,
            base_url=s.intent_classifier_base_url,
            timeout_seconds=s.intent_classifier_timeout_seconds,
        )
        if s.intent_classifier_enabled
        else None
    )
    gate = IntentGate(classifier, s.intent_classifier_confidence_threshold)

    product_inquiry = ProductInquiryService(
        customer_repository, product_repository, price_repository, inventory_repository
    )
    orders = OrderService(
        customer_repository,
        product_repository,
        price_repository,
        order_repository,
        inventory_repository,
    )
    executor = build_tool_executor(
        ToolServices(product_inquiry=product_inquiry, orders=orders)
    )
    provider = SurplussProvider(
        api_key=s.openai_api_key,
        model=s.openai_model,
        base_url=s.openai_base_url,
    )
    orchestrator = AgentOrchestrator(provider, executor)

    adapter = (
        WhatsAppAdapter(
            bridge_url=s.whatsapp_bridge_url,
            token=s.whatsapp_bridge_token,
            timeout_seconds=s.whatsapp_bridge_timeout_seconds,
        )
        if s.whatsapp_bridge_url
        else None
    )
    outbox = OutboxProcessor(
        outbox_repository,
        conversation_repository,
        approvals,
        adapter,
        stale_after_seconds=s.outbox_stale_sending_seconds,
    )

    processor = MessageProcessor(
        conversations=conversations,
        gate=gate,
        orchestrator=orchestrator,
        handoff=handoff,
        approvals=approvals,
        outbox=outbox,
        max_intent_context=s.intent_classifier_max_context_messages,
        max_transcript_messages=s.agent_transcript_max_messages,
    )
    return MessagingStack(
        processor=processor,
        conversations=conversations,
        handoff=handoff,
        outbox=outbox,
    )
