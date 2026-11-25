import django_filters
from django.db.models import OuterRef, Q, Subquery
from rest_framework import generics
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from chatbot.chat_auth import IsVerifiedCaller, VerifiedTokenAuthentication
from chatbot.filter.drf_filter import ChatSessionProfileFilter
from chatbot.parsers import StrictJSONParser
from chatbot.models import ChatSession, BotVernacular, SessionFlowName
from chatbot.models.company_models import CompanyChat, CompanyChatFeedback, CompanyBot, Flow
from chatbot.serializer.base_serializer import ChatSessionSerializer
from chatbot.serializer.company_serializer import (
    CompanyBotSerializer, BotVernacularSerializer,
    FlowLanguagesSerializer, FlowConnectionInfoSerializer
)
from chatbot.serializer.profile_serializer import (
    CompanyChatSerializer, CompanyChatFeedbackSerializer
)


def _with_latest_feedback(queryset):
    """Annotate each row with its latest feedback's thumbs_up/thumbs_down instead of
    prefetching the full (potentially unbounded, append-only) feedback history."""
    latest_feedback = CompanyChatFeedback.objects.filter(
        company_chat=OuterRef('pk')
    ).order_by('-created_at')
    return queryset.annotate(
        latest_thumbs_up=Subquery(latest_feedback.values('thumbs_up')[:1]),
        latest_thumbs_down=Subquery(latest_feedback.values('thumbs_down')[:1]),
    )


def _owned_sessions(user_id):
    """Sessions that belong to a user. A session made by create_chatsession carries only a
    profile; one made by the websocket path carries user_id (and often a profile)."""
    return ChatSession.objects.filter(Q(user_id=user_id) | Q(profile__userid=user_id))


class OwnerOnlyMixin:
    """Verified login required; reads are limited to the caller's own rows (T32, ISS-45).
    Read-only: the frontend never writes to these paths."""
    authentication_classes = [VerifiedTokenAuthentication]
    permission_classes = [IsVerifiedCaller]

    def owned_sessions(self):
        return _owned_sessions(self.request.user.user_id)


class OwnedChatMixin(OwnerOnlyMixin):
    def get_queryset(self):
        # CompanyChat.session is a plain string, not a foreign key, so match on the owner's session strings.
        return _with_latest_feedback(
            CompanyChat.objects.filter(session__in=self.owned_sessions().values('session'))
        )


class CompanyChatListCreateView(OwnedChatMixin, generics.ListAPIView):
    serializer_class = CompanyChatSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['message', 'sender', 'receiver', 'session', 'status']

    def get_queryset(self):
        return super().get_queryset().order_by('created_at')


class CompanyChatRetrieveUpdateDestroyView(OwnedChatMixin, generics.RetrieveAPIView):
    serializer_class = CompanyChatSerializer


class CompanyChatFeedbackCreateView(generics.CreateAPIView):
    """Create-only: FE always POSTs a new feedback row, never PATCH/PUT an existing one."""
    queryset = CompanyChatFeedback.objects.all()
    serializer_class = CompanyChatFeedbackSerializer
    parser_classes = [StrictJSONParser]


class CompanyBotListCreateView(generics.ListAPIView):
    """Public chat metadata only. Configuration is managed in Django admin."""

    permission_classes = [AllowAny]
    queryset = CompanyBot.objects.all()
    serializer_class = CompanyBotSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['name', 'company__name', 'company__slug', 'route']


class CompanyBotRetrieveUpdateDestroyView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    queryset = CompanyBot.objects.all()
    serializer_class = CompanyBotSerializer


class BotVernacularListCreateView(generics.ListAPIView):
    permission_classes = [AllowAny]
    queryset = BotVernacular.objects.all()
    serializer_class = BotVernacularSerializer
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['company_bot', 'language', 'company_bot__route']


class BotVernacularRetrieveUpdateDestroyView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    queryset = BotVernacular.objects.all()
    serializer_class = BotVernacularSerializer


def _annotate_first_user_message(qs):
    first_msg_sq = (
        CompanyChat.objects
        .filter(session=OuterRef('session'))
        .exclude(sender_id=1)
        .order_by('created_at')
        .values('message')[:1]
    )
    return qs.annotate(first_user_message=Subquery(first_msg_sq))


class OwnedSessionMixin(OwnerOnlyMixin):
    serializer_class = ChatSessionSerializer

    def get_queryset(self):
        return _annotate_first_user_message(self.owned_sessions())


class ChatSessionListCreateView(OwnedSessionMixin, generics.ListAPIView):
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend, ChatSessionProfileFilter, OrderingFilter]
    filterset_fields = ['session', 'project_id', 'user_id', 'profile', 'session_type']
    ordering_fields = ['updated_at', 'created_at', 'id']
    ordering = ['-updated_at']


class ChatSessionRetrieveUpdateDestroyView(OwnedSessionMixin, generics.RetrieveAPIView):
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]
    filterset_fields = ['session']


class ChatSessionRetrieveUpdateDestroyViewSession(OwnedSessionMixin, generics.RetrieveAPIView):
    lookup_field = 'session'


class FlowLanguagesView(generics.GenericAPIView):
    """
    API endpoint to get supported languages for a specific flow route.
    Query param: flow_route (required)
    Returns: List of language codes
    """
    serializer_class = FlowLanguagesSerializer
    
    def get(self, request, *args, **kwargs):
        flow_route = request.query_params.get('flow_route')
        
        if not flow_route:
            return Response(
                {'error': 'flow_route query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            flow = Flow.objects.get(
                flow_route=flow_route,
                active=True
            )
            
            serializer = self.get_serializer(flow)
            return Response(serializer.data)
            
        except Flow.DoesNotExist:
            return Response(
                {'error': 'Flow not found or inactive'},
                status=status.HTTP_404_NOT_FOUND
            )


class FlowConnectionInfoView(generics.GenericAPIView):
    """
    API endpoint to get websocket URL and bot route for a flow.
    Query param: flow_route (required)
    Returns: websocket_url, bot route, isParentFlow flag, children flows, and image configuration
    """
    serializer_class = FlowConnectionInfoSerializer
    
    def get(self, request, *args, **kwargs):
        flow_route = request.query_params.get('flow_route')
        
        if not flow_route:
            return Response(
                {'error': 'flow_route query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            flow = Flow.objects.select_related('bot', 'image_config').prefetch_related('child_flows').get(
                flow_route=flow_route
            )
            
            # Check if flow is active
            if not flow.active:
                return Response(
                    {'error': 'Flow is inactive'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            serializer = self.get_serializer(flow)
            return Response(serializer.data)
            
        except Flow.DoesNotExist:
            return Response(
                {'error': 'Flow not found'},
                status=status.HTTP_404_NOT_FOUND
            )
