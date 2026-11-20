"""
MIP (Micro Improvement Plan) tracking models, Saathi 1.5.0.

Design: Plans/SAATHI_1.5.0_MIP_Tracking_README.md, section 4 (database design).

    chatbot_flow ──1:N── chatbot_mipcheckinrule
                                  ▲ PROTECT (mip.next_check_in_rule)
    chatbot_profile ──1:N──┐      │            ┌──1:1── chatbot_mipdetails
                           ├─(PROTECT)─ chatbot_mip ┤
    chatbot_chatsession ─1:N┘                   └──1:1── chatbot_mipstory

All four models keep change history (simple_history), so each save also writes
a row to chatbot_historical<model>. Nothing here changes an existing table.
Related models are referenced by string ('chatbot.Flow', ...) to avoid the
import-order cycle in chatbot/models/__init__.py.
"""
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from chatbot.models.enums import (
    MipCheckInSchedule, MipCheckInTrigger, MipStatus, MipStoryProvider, MipStoryStatus,
)

__all__ = ['MipCheckInRule', 'Mip', 'MipDetails', 'MipStory']


class MipCheckInRule(models.Model):
    """
    Configuration: one kind of question Saathi asks later about a MIP (design 4.6).
    Edited in Django admin and loaded by `sync_saathi_bot`, so cadence, copy and
    chips change with no code and no migration. A MIP only points at the rule it
    owes next (Mip.next_check_in_rule).
    """
    flow = models.ForeignKey(
        'chatbot.Flow',
        on_delete=models.PROTECT,
        related_name='mip_check_in_rules',
        help_text="Flow these rules belong to. A flow can't be deleted while it has rules.",
    )
    code = models.CharField(
        max_length=50,
        help_text="Stable name used by code and logs, unique per flow (e.g. MIDPOINT_CHECK_IN).",
    )
    name = models.CharField(
        max_length=255,
        help_text="Admin label (e.g. 'Midpoint progress check').",
    )
    trigger = models.CharField(
        max_length=30,
        choices=MipCheckInTrigger.choices,
        help_text="Event that starts this rule's clock.",
    )
    schedule_type = models.CharField(
        max_length=30,
        choices=MipCheckInSchedule.choices,
        help_text="How due dates are worked out. Exactly the matching parameter "
                  "(day offsets / every N days / percent of timeline) must be set.",
    )
    day_offsets = models.JSONField(
        null=True,
        blank=True,
        help_text="DAY_OFFSETS only: days after the trigger, e.g. [1, 2, 9, 16].",
    )
    every_n_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="EVERY_N_DAYS only: repeat every N days until the plan ends.",
    )
    percent_of_timeline = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="PERCENT_OF_TIMELINE only: point in the plan (50 = midpoint, 100 = end).",
    )
    max_asks = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Stop after this many asks. Empty = no limit.",
    )
    priority = models.PositiveSmallIntegerField(
        default=0,
        help_text="Order when several rules are due the same day.",
    )
    message_template = models.TextField(
        help_text="Message copy with placeholders {title}, {start_date}, {end_date}; "
                  "translated to the user's language.",
    )
    quick_reply_chips = models.JSONField(
        default=list,
        blank=True,
        help_text="Quick-reply chips (list of strings), translated.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Switch the rule off without deleting it.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"{self.code} ({self.name})"

    class Meta:
        verbose_name = "MIP check-in rule"
        verbose_name_plural = "MIP check-in rules"
        constraints = [
            models.UniqueConstraint(
                fields=['flow', 'code'],
                name='uniq_mipcheckinrule_flow_code',
            ),
            # Exactly the parameter for the chosen schedule is set; the other two are empty.
            models.CheckConstraint(
                condition=(
                    Q(schedule_type=MipCheckInSchedule.DAY_OFFSETS, day_offsets__isnull=False,
                      every_n_days__isnull=True, percent_of_timeline__isnull=True)
                    | Q(schedule_type=MipCheckInSchedule.EVERY_N_DAYS, day_offsets__isnull=True,
                        every_n_days__isnull=False, percent_of_timeline__isnull=True)
                    | Q(schedule_type=MipCheckInSchedule.PERCENT_OF_TIMELINE, day_offsets__isnull=True,
                        every_n_days__isnull=True, percent_of_timeline__isnull=False)
                ),
                name='mipcheckinrule_one_schedule_param',
            ),
        ]


class Mip(models.Model):
    """
    One row per generated MIP: identity + tracking state (design 4.3).
    Created only when the plan is complete and both PDF and DOCX were rendered.
    `id` is the MIP ID used in every API, the Mitra redirect and by Mitra.
    """
    profile = models.ForeignKey(
        'chatbot.Profile',
        on_delete=models.PROTECT,
        related_name='mips',
        help_text="Saathi's internal user record. The profile can't be deleted while it owns a MIP.",
    )
    user_id = models.CharField(
        max_length=400,
        help_text="Common-login (Elevate) user ID; same as ChatSession.user_id and the token's data.id.",
    )
    chat_session = models.ForeignKey(
        'chatbot.ChatSession',
        on_delete=models.PROTECT,
        related_name='mips',
        help_text="Chat where the MIP was created. Every later question is asked in this chat, "
                  "and the sidebar indicator shows on this chat.",
    )
    title = models.CharField(
        max_length=255,
        help_text="Plan title (PDF heading); used wherever the copy says [focus area].",
    )
    status = models.CharField(
        max_length=20,
        choices=MipStatus.choices,
        default=MipStatus.GENERATED,
        help_text="Lifecycle; decides what may be asked and whether the chat shows the indicator.",
    )
    status_changed_at = models.DateTimeField(
        default=timezone.now,
        help_text="When `status` last changed.",
    )
    start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Day the user says they start. Editable until `end_date` is saved. Never set by the system.",
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Day the user says they finished. Set only together with status COMPLETED.",
    )
    planned_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="start_date + duration_days - 1: when the plan's timeline ends.",
    )
    next_check_in_rule = models.ForeignKey(
        MipCheckInRule,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='pending_mips',
        help_text="Which question Saathi owes next for this MIP. Empty = nothing to ask.",
    )
    next_check_in_on = models.DateField(
        null=True,
        blank=True,
        help_text="Day that question becomes due (asked in the first session on or after it).",
    )
    check_in_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="How many times the current rule has been asked; resets when the rule changes.",
    )
    last_check_in_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Saathi last asked anything for this MIP (at most one ask per day).",
    )
    is_tracking_declined = models.BooleanField(
        default=False,
        help_text="User firmly said no to tracking; no check-ins.",
    )
    help_note_shown = models.BooleanField(
        default=False,
        help_text="Whether the one-time help note was sent.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"MIP {self.pk}: {self.title}"

    class Meta:
        verbose_name = "MIP"
        verbose_name_plural = "MIPs"
        # chat_session_id is indexed by its FK (also serves the sidebar-indicator lookup);
        # user_id is the leading column of (user_id, status).
        indexes = [
            models.Index(fields=['user_id', 'status'], name='chatbot_mip_user_status_idx'),
            models.Index(
                fields=['next_check_in_on'],
                condition=Q(next_check_in_rule__isnull=False),
                name='chatbot_mip_due_check_in_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=MipStatus.STARTED) | Q(start_date__isnull=False),
                name='mip_started_has_start_date',
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=MipStatus.COMPLETED, end_date__isnull=False)
                    | (~Q(status=MipStatus.COMPLETED) & Q(end_date__isnull=True))
                ),
                name='mip_completed_iff_end_date',
            ),
            models.CheckConstraint(
                condition=(
                    Q(next_check_in_rule__isnull=True, next_check_in_on__isnull=True)
                    | Q(next_check_in_rule__isnull=False, next_check_in_on__isnull=False)
                ),
                name='mip_next_check_in_both_or_neither',
            ),
        ]


class MipDetails(models.Model):
    """
    The plan content of a MIP, 1:1 with Mip (design 4.4), taken from the
    `download_file` tool arguments. Locked after generation: only an action
    step's `status` may change. User and chat are reached through `mip`
    (details.mip.user_id), not copied, so they can't drift apart.
    """
    mip = models.OneToOneField(
        Mip,
        on_delete=models.CASCADE,
        related_name='details',
        help_text="Owner MIP.",
    )
    goal = models.TextField(
        help_text="Confirmed goal statement (tool `goal`). Sent to Mitra as the challenge.",
    )
    objective = models.TextField(
        help_text="SMART objective(s) (tool `objective`).",
    )
    duration_days = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Plan length in days, parsed from tool `duration` (fallback: last step's week x 7).",
    )
    action_steps = models.JSONField(
        help_text='Steps as generated, list order = step number: [{"action", "week", "status"}]. '
                  'Text is read-only; only `status` changes (PENDING -> COMPLETED).',
    )
    success_indicators = models.JSONField(
        help_text="Tool `success_indicators` (list of strings).",
    )
    files = models.JSONField(
        help_text='MIP download links (S3); must contain "pdf" and "docx".',
    )
    language = models.CharField(
        max_length=10,
        help_text="Language the plan was generated in (e.g. kn).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"Details of MIP {self.mip_id}"

    class Meta:
        verbose_name = "MIP details"
        verbose_name_plural = "MIP details"
        constraints = [
            models.CheckConstraint(
                condition=Q(duration_days__gt=0),
                name='mipdetails_duration_days_gt_0',
            ),
            models.CheckConstraint(
                condition=Q(files__has_keys=['pdf', 'docx']),
                name='mipdetails_files_has_pdf_docx',
            ),
        ]


class MipStory(models.Model):
    """
    The celebratory report for a MIP, 1:1 with Mip (design 4.5). Provider-neutral:
    `provider` + `external_session_id` identify the external app (Mitra today) and
    its own session. Locked once GENERATED. Its existence answers "does a report
    already exist?". There is no "shown in chat" column by design.
    """
    mip = models.OneToOneField(
        Mip,
        on_delete=models.CASCADE,
        related_name='story',
        help_text="One report per MIP.",
    )
    status = models.CharField(
        max_length=20,
        choices=MipStoryStatus.choices,
        help_text="DECLINED (user refused), IN_PROGRESS (started with the provider) or GENERATED (final, locked).",
    )
    provider = models.CharField(
        max_length=30,
        choices=MipStoryProvider.choices,
        null=True,
        blank=True,
        help_text="External app that captured the story. Empty only for a DECLINED row that never went to a provider.",
    )
    external_session_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="The provider's own session/story ID (for Mitra: Story.session).",
    )
    language = models.CharField(
        max_length=10,
        help_text="Story language (e.g. kn).",
    )
    captured_elements = models.JSONField(
        default=dict,
        blank=True,
        help_text='The four elements (impact, challenges_highlights, how_overcome, who_helped), '
                  'each {"text", "source", "original_text"?}.',
    )
    story_text = models.TextField(
        null=True,
        blank=True,
        help_text="Story narrative.",
    )
    files = models.JSONField(
        null=True,
        blank=True,
        help_text='Report downloads; "pdf" required once GENERATED, "docx" if the provider supplies one.',
    )
    generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the final report arrived; the report is locked from here.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"Story of MIP {self.mip_id} ({self.status})"

    class Meta:
        verbose_name = "MIP story"
        verbose_name_plural = "MIP stories"
        constraints = [
            # files IS NOT NULL is explicit: `NULL ? 'pdf'` is NULL, which a CHECK would let through.
            models.CheckConstraint(
                condition=(
                    ~Q(status=MipStoryStatus.GENERATED)
                    | Q(files__isnull=False, files__has_key='pdf', generated_at__isnull=False)
                ),
                name='mipstory_generated_has_pdf_and_time',
            ),
            models.CheckConstraint(
                condition=Q(external_session_id__isnull=True) | Q(provider__isnull=False),
                name='mipstory_session_requires_provider',
            ),
            # Two providers could issue the same session ID. NULLs are distinct,
            # so DECLINED rows without a provider don't collide.
            models.UniqueConstraint(
                fields=['provider', 'external_session_id'],
                name='uniq_mipstory_provider_session',
            ),
        ]
