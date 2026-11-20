#!/usr/bin/env python3
"""Generate public TypeScript contracts from the server's Pydantic JSON schemas."""
from pathlib import Path
import json
import sys
import typing
from pydantic import TypeAdapter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.contracts import schemas as s

ROOT = Path(__file__).resolve().parents[1]
CLASSES = [s.DisplaySettingsRestore, s.PersonPropertiesInput, s.ContactTypeInput, s.PersonContactInput, s.PersonCard, s.PlanningGroupInput, s.PlanningSettingsInput, s.PlanningRollback, s.ScoreSettingsInput, s.ScoreSettingsRollback, s.ScoreReview, s.LegacyLeadCheckInput, s.JobRecovery, s.LeadContextInput, s.LeadContext, s.Me, s.RuleCheckComparison, s.PolicyActivationDecision, s.JobInput, s.JobCancelInput, s.GenerationExtension, s.JobTimeInput, s.GenerationContinue, s.JobResult, s.GenerationApply, s.ImportPreview, s.ImportApply, s.ImportRefresh, s.StaffImportPreview, s.StaffImportReview, s.StaffAccessBulkInput, s.CreatedUndo, s.ReferenceRotaPreview, s.DepartmentSettingsInput, s.StaffingSuggestionsInput, s.FillOptionsInput, s.FillOptionsResult, s.WhyInput, s.IssueBatch, s.IssuePreviewInput, s.ExceptionBatch, s.CategoryInput, s.DisplaySettingsInput, s.SeriesReconcile, s.SeriesExpand, s.OperatingCalendarInput, s.ResourcePoolInput, s.ResourceRequirement, s.PlannerViewInput, s.PlannerViewDefinition, s.PlannerViewSettings, s.FairnessViewSettings, s.RulesViewSettings, s.AssignmentTypeInput, s.PolicyInput, s.SeriesInput, s.ShiftInput, s.Command,
           s.LivePulse, s.WorkspaceCompactDetail, s.WorkspacePartQuery, s.WorkspacePartPage,
           s.DraftEditDelta,
           s.MembershipInput, s.ProgrammeMembershipInput, s.ProgrammeCalendarInput, s.MentorshipInput, s.RotationInput, s.EligibilityInput, s.GrantInput, s.AvailabilityInput,
           s.RotationBoard, s.RotationBlock, s.RotationTemplateInput, s.RotationPlanInput, s.RotationCopyInput, s.RotationRemoveInput,
           s.RotationChangeInput, s.RotationSwapInput, s.TraineeNoteInput, s.TraineeNoteEdit,
           s.WorkspaceInput, s.RotaSummary, s.RotaCauseSummary, s.PublishedScheduleSummary, s.LocationInput, s.RoleInput, s.CustomField, s.LeaveTypeInput, s.ParticipantRecord,
           s.ReviewRouteInput, s.ReviewDelegationInput, s.ReviewDecisionInput, s.AuthorityRestoreInput,
           s.SwapEligibility, s.SwapOfferInput, s.SwapCounterofferInput, s.SwapPrepareInput, s.PersonInput, s.TagInput, s.TagMembersInput, s.TagMemberChange, s.TagMemberPreviewInput, s.ServiceInput, s.DepartmentContentInput, s.DepartmentContentSeen, s.LeaveInput, s.StaffWishInput, s.RequestRotaLink, s.SelfApprovalInput,
           s.WishOverrideInput, s.AuthConfig, s.IdentifierInput, s.IdentifierResponse, s.AuthSettings, s.AuthConfigInput, s.AuthConfigChange, s.AuthConfigStatus, s.AuthConnectionTest, s.AuthConnectionResult, s.LoginInput, s.LoginResponse, s.OIDCContinuation, s.OIDCDepartmentChoice, s.OIDCDepartment, s.OIDCDepartmentChoices, s.OIDCDepartmentSession, s.AccountInvite, s.SessionSettingsInput, s.PasswordSet, s.PasswordChange, s.RuleProposalInput, s.RuleMapProposal, s.RuleMigrationInput, s.RuleMigrationBlocker, s.RuleAcknowledgementInput,
           s.RulesExplainedResponse, s.DepartmentRulesExplainedResponse, s.ExplainedRule, s.RuleExplainedSentence,
            s.SchedulingFactInput, s.FactDecisionInput, s.SchedulingFactApproval, s.CatalogueConversionDecision, s.CatalogueProofPreview, s.CatalogueUpdating, s.JobErrorDetail,
           s.PersonFairness, s.PersonScore, s.PersonWorkload, s.PersonRotations, s.PersonAvailability, s.PersonAccounts, s.PersonSessions, s.PersonAuthActivity,
           s.PublishedWishOutcome, s.RequestDecisionAccess, s.RequestManyDecision, s.RequestUndo, s.OvertimeAvailabilityInput, s.NotificationRead, s.WishEffect, s.DraftWishOutcome, s.FeedbackCreate, s.FeedbackCreated, s.FeedbackRow,
           s.FeedbackList, s.FeedbackUpdate, s.FeedbackUndo, s.FeedbackExport, s.FeedbackExportResult, s.WhyResult,
           s.DateSetInput, s.DateSetRecord, s.DateSetMarker, s.ResolvedDateSetEntry, s.DateSetDeleteInput, s.DateSetEntryWriteInput,
           s.DateSetEntryDeleteInput, s.DateSetAdjustmentInput, s.DateSetImportInput, s.DateSetCopyInput,
           s.GithubIntegrationStatus, s.GithubConnect, s.GithubDisconnect, s.GithubTest,
           s.MonitorTokenInput, s.MonitorIntegrationStatus, s.MonitorTokenResult, s.MonitorEvent, s.MonitorIncident,
           s.MonitorEventList, s.MonitorEventReceipt, s.SystemStatusFiles,
           s.EventTypeInput, s.EventInput, s.EventOccurrenceEdit, s.EventRSVP, s.EventOccurrenceRow, s.EventOccurrenceList, s.ScheduleEventRow,
           s.EventReceiptList, s.CalendarFeedSettingsInput, s.EmailSettingsInput]
CLASSES += [s.AvailabilityUpdate, s.AvailabilityUndoUpdate, s.RelationshipUpdateDates, s.BulkSettings, s.BulkSettingsInput,
            s.BulkApply, s.BulkApplyResult, s.BulkItem, s.BulkChange, s.BulkSelection, s.BulkResolved, s.BulkCollections,
            s.BulkPreview, s.BulkRowsInput, s.BulkRows, s.BulkStageInput, s.BulkStagedRow, s.BulkStagedList]
CLASSES += [s.BulkRuleLine, s.BulkRuleLinesInput, s.BulkTagActionInput, s.TableViewDefinition, s.TableViewInput,
            s.TableViewDefaultInput, s.TableViewDefaults, s.CollectionFilter]
CLASSES += [s.ConditionResolveInput, s.ConditionDateParseInput, s.DepartmentCreateInput, s.DepartmentCreatePreview]
CLASSES += [s.RuleCatalogueInForceEntry, s.RuleCatalogueInForce]
CLASSES += [s.CatalogueCounts]
CLASSES += [s.ConditionResolution, s.ConditionDateMatch, s.ConditionDateParseResult]
CLASSES += [s.ConditionResolveBatchInput, s.ConditionResolveBatchResult, s.TimeResolveBatchInput, s.TimeResolveBatchResult]
CLASSES += [s.FairnessExposure]
CLASSES += [s.FixListDelta]
CLASSES += [s.ObservedRuleEffect, s.CatalogueWorkspaceImpact]
CLASSES += [s.NotificationList, s.NotificationReadResult, s.NotificationUnack, s.AuditPage]
CLASSES += [s.PublishedOperationalSummary, s.PublishedOperationalAttention, s.FairnessMetricTotal]
CLASSES += [s.HomeRotaList, s.DraftMove, s.ShiftAction, s.AvailabilityUndoCreate, s.RuleRelaxInput, s.RuleAdjustmentInput, s.AdjustmentEffectInput]
CLASSES += [s.PolicyVersionApproval, s.ExceptionApproval]
CLASSES += [s.SeriesOverlapAcknowledgement, s.CatalogueBulk, s.SeriesPreviewInput, s.SeriesOverlapPreview,
            s.SeriesOverlapBatchPreview, s.SeriesOccurrenceCancel, s.SessionOverlapFlags, s.CatalogueConflictDetails]
CLASSES += [s.RotationTypeInput, s.TimelineResponse, s.TimelineRotationInput, s.TimelinePreview, s.TimelineRotationResult]
CLASSES += [s.AutoscheduleProgress, s.CoverageInterval]
CLASSES += [s.WorkspaceMergePreviewInput, s.WorkspaceMergeApplyInput]
CLASSES += [s.FixList, s.FixListLean, s.FixListDetail, s.WorkspaceSubmitInput]
CLASSES += [s.JobPlanningContext, s.RuleLineValidity, s.RuleAttentionCounts, s.SolverExclusionCount,
            s.PlanningContextInput, s.RequirementEvidenceInput, s.PeriodReadiness, s.RequirementEvidence]
CLASSES += [s.BrandingSettings, s.BrandingPublic, s.BrandingInput, s.BrandingStatus, s.BrandingImage, s.GenerationControls]
CLASSES += [s.PwaManifest, s.PwaStatus, s.PushSubscribeInput, s.PushRotateInput, s.PushUnsubscribeInput, s.PushTestInput,
            s.NotificationPolicyInput, s.NotificationPreferencesInput, s.PwaRollbackInput, s.PwaSystemInput, s.PwaProviderResumeInput]
CLASSES += [s.RuleLineEffectInput, s.RuleRelation, s.PromoteAdjustmentInput, s.PromoteAdjustmentSeed, s.RotaExtraction, s.ScoreCheckUpdateInput, s.ScoreCheckGapDecisionInput,
            s.ScoreCheckUndoInput, s.ScoreCheckSaveInput, s.ScoreCheckBatch, s.ScoreCheckAutoInput,
            s.ScoreCheckBenchmarkInput, s.ScoreCheckBenchmarkResult, s.AiProviderProfileInput,
            s.AiProviderKeyInput, s.AiProviderProfileRecord, s.AiCallRecord]
CLASSES += [s.ScoreImport, s.ImportCheck, s.ImportMatch, s.ImportMatches, s.Benchmark, s.ScoreImportUpload, s.ScoreImportAddFiles,
            s.ScoreImportUpdateSource, s.ScoreImportSourceAction, s.ScoreImportSetTime, s.ScoreImportProfile,
            s.ScoreImportAction, s.ScoreImportDecide, s.ScoreImportSave, s.ScoreImportSaved,
            s.AiSettings, s.AiEnabledInput, s.ScoreProviderChoice, s.ScoreProviderChoices]
CLASSES += [s.PublicationChangeInput, s.PublicationChangeUndo, s.PublicationChangesSubmit, s.AbsenceReportInput, s.AbsenceReportSettingsInput, s.OwnAbsenceReportSummary]
CLASSES += [s.PublicationAudiencePreview]
for collection in s.BULK_COLLECTIONS.values():
    cls = getattr(s, collection['schema']) if collection['schema'] else None
    if cls is not None and cls not in CLASSES:
        CLASSES.append(cls)


def ts(schema):
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(x) for x in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(ts(x) for x in schema[key])
    kind = schema.get("type")
    if kind in {"integer", "number"}: return "number"
    if kind == "boolean": return "boolean"
    if kind == "null": return "null"
    if kind == "string": return "File" if schema.get("format") == "binary" else "string"
    if kind == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(ts(item) for item in schema["prefixItems"]) + "]"
        return "Array<" + ts(schema.get("items", {})) + ">"
    if kind == "object" or "properties" in schema:
        if not schema.get("properties"):
            values = schema.get("additionalProperties", {})
            if not values and len(schema.get("patternProperties", {})) == 1:
                values = next(iter(schema["patternProperties"].values()))
            record = "Record<" + ts(schema.get("propertyNames", {"type": "string"})) + ", " + ts(values if isinstance(values, dict) else {}) + ">"
            return "Partial<" + record + ">" if "propertyNames" in schema else record
        required = set(schema.get("required", []))
        return "{\n" + "\n".join("  " + json.dumps(k) + ("" if k in required else "?") + ": " + ts(v) + ";" for k, v in schema["properties"].items()) + "\n}"
    return "unknown"


def references(value):
    if isinstance(value, dict):
        return ({value['$ref'].split('/')[-1]} if '$ref' in value else set()).union(
            *(references(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(references(child) for child in value))
    return set()


def render():
    definitions = {}
    for name in ("DraftSafetyChange", "DraftSafetyUseDefault", "DraftConfigEditors", "DraftConfigApproval",
                 "DraftConfigSource", "DraftDefaultChange", "DraftConfigReason", "DraftSettingsStatus"):
        definitions[name] = TypeAdapter(getattr(s, name)).json_schema()
    schemas = {}
    for cls in CLASSES:
        schema = cls.model_json_schema()
        definitions.update(schema.pop("$defs", {}))
        if schema.get("$ref") == "#/$defs/" + cls.__name__:
            schema = definitions[cls.__name__]
        schemas[cls.__name__] = schema
    definitions.update(schemas)
    # Condition output always includes values, including [] for operand-free/new scalar lines. Retain the existing
    # frontend's required output property while the input schema allows callers to omit its empty default.
    for name in ('Condition', 'CollectionCondition'):
        definitions[name] = {**definitions[name],
                             'required': list(dict.fromkeys([*definitions[name].get('required', []), 'values']))}
    links = {name: references(schema) for name, schema in definitions.items()}

    def recursive(name, pending, seen):
        return any(child == name or child not in seen and recursive(name, links.get(child, ()), seen | {child})
                   for child in pending)

    content = "// GENERATED by scripts/generate_contracts.py. Do not edit by hand.\n\n"
    # Object interfaces provide the named boundary TypeScript requires for recursive contracts.
    content += "\n\n".join(("export interface " + name + " extends Record<string, unknown> " + ts(schema)
                           if schema.get('properties') and recursive(name, links[name], {name}) else
                           "export type " + name + " = " + ts(schema) + ";")
                          for name, schema in sorted(definitions.items())) + "\n"
    # Shared lists the screens iterate over (roles, guarded actions): one definition, in the contracts.
    # The release this web bundle is built from (apps/api/version.py): About Rota compares it with the running API.
    from apps.api.version import __version__
    content += "\nexport const APP_VERSION = " + json.dumps(__version__) + ";\n"
    content += "export const ROLE_NAMES = " + json.dumps(list(s.ROLE_NAMES)) + " as const;\n"
    content += "export type JobCurrency = " + " | ".join(json.dumps(value) for value in typing.get_args(s.JobCurrency)) + ";\n"
    content += "export const RUN_SUPERSEDED = " + json.dumps(s.RUN_SUPERSEDED) + " as const;\n"
    content += "export const LIVE_RECHECK_BASIS = " + json.dumps(s.LIVE_RECHECK_BASIS) + " as const;\n"
    content += "export const DRAFT_CONFIG_DEFAULTS = " + json.dumps(s.DRAFT_CONFIG_DEFAULTS) + " as const;\n"
    content += "export const ABSENCE_CATEGORIES = " + json.dumps(list(s.ABSENCE_CATEGORIES)) + " as const;\n"
    content += "export const ABSENCE_REPORT_LABEL = " + json.dumps(s.ABSENCE_REPORT_LABEL) + ";\n"
    content += "export const ABSENCE_REPORTER_CATEGORIES = " + json.dumps(list(s.ABSENCE_REPORTER_CATEGORIES)) + " as const;\n"
    content += "export const ABSENCE_CATEGORY_LABELS = " + json.dumps(s.ABSENCE_CATEGORY_LABELS) + " as const;\n"
    content += "export const UNAVAILABLE_OUTCOME = " + json.dumps(s.UNAVAILABLE_OUTCOME, sort_keys=True) + " as const;\n"
    content += "export type LiveScope = LiveChange['scope'];\n"
    content += "export const PWA_NOTIFICATION_EVENTS = " + json.dumps(s.PWA_NOTIFICATION_EVENTS) + " as const;\n"
    content += "export const PUBLICATION_AUDIENCE_REASON_LABELS = " + json.dumps(s.PUBLICATION_AUDIENCE_REASON_LABELS) + " as const;\n"
    content += "export type PublicationAudienceReason = NotificationAudienceRecipient['reasons'][number];\n"
    content += "export const PWA_DETAIL_LEVELS = " + json.dumps(s.PWA_DETAIL_LEVELS) + " as const;\n"
    content += "export const AUDIT_ACTION_CATALOGUE = " + json.dumps(s.AUDIT_ACTION_CATALOGUE) + " as const;\n"
    content += "export const AUDIT_GROUPS = " + json.dumps(s.AUDIT_GROUPS) + " as const;\n"
    content += "export const GENERATION_WEIGHT_PRESETS = " + json.dumps(s.GENERATION_WEIGHT_PRESETS) + " as const;\n"
    for name in ('USUAL_ASSIGNEES_FIXED_DEFAULT', 'WEEKDAY_SHORT_LABELS', 'STRENGTH_LABELS',
                 'SCORE_WEIGHT_LABELS', 'SCORE_WEIGHT_HELP', 'RULE_KINDS', 'RULE_HELP',
                 'RULE_NUMBER_FIELD', 'RULE_TOPICS', 'FINDING_MESSAGES', 'TEAM_PROBLEM_WORDS', 'ISSUE_LIST_LIMIT', 'GENERATION_SCOPE_CAPABILITY', 'CONTACT_KIND_ACTIONS',
                 'FIX_ACTION_LABELS', 'FIX_ACTION_GROUPS', 'FIX_GROUP_LABELS'):
        content += "export const " + name + " = " + json.dumps(getattr(s, name)) + " as const;\n"
    content += "export const ROTATION_BLOCK_UNITS = " + json.dumps(list(s.ROTATION_BLOCK_UNITS)) + " as const;\n"
    # Auto-schedule animation settings: defaults, number bounds and choices read from the schema's own fields.
    def bounds(model):
        found = {}
        for field_name, info in model.model_fields.items():
            low = next((m.ge for m in info.metadata if getattr(m, 'ge', None) is not None), None)
            high = next((m.le for m in info.metadata if getattr(m, 'le', None) is not None), None)
            if low is not None and high is not None:
                found[field_name] = [low, high]
        return found

    animation = (s.AutoscheduleAnimationSettings, s.AutoscheduleBigMoveSettings)
    content += "export const AUTOSCHEDULE_ANIMATION_DEFAULTS = " + json.dumps(s.AutoscheduleAnimationSettings().model_dump(mode='json')) + " as const;\n"
    content += "export const AUTOSCHEDULE_ANIMATION_LIMITS = " + json.dumps({k: v for m in animation for k, v in bounds(m).items()}) + " as const;\n"
    content += "export const AUTOSCHEDULE_ANIMATION_CHOICES = " + json.dumps({k: list(typing.get_args(f.annotation)) for m in animation
                                                                              for k, f in m.model_fields.items() if typing.get_origin(f.annotation) is typing.Literal}) + " as const;\n"
    # Wish priority settings (Configuration › Auto-schedule › Staff wishes): their words, defaults and bounds, from the schema.
    def described(model, names):
        out = {}
        for name in names:
            info = model.model_fields[name]
            meta = {k: next((getattr(m, k) for m in info.metadata if getattr(m, k, None) is not None), None) for k in ('ge', 'le')}
            out[name] = {'title': info.title, 'help': info.description, 'default': info.default, 'min': meta['ge'], 'max': meta['le'],
                         'integer': info.annotation is int}
        return out
    content += "export const WISH_PRIORITY_FIELDS = " + json.dumps(described(s.ScoringSettings, ('wish_priority_decay', 'wish_granted_credit', 'wish_history_periods'))) + " as const;\n"
    # A run's stop conditions (Stop when: Configuration › Timing and one run): words, defaults and bounds; the words for
    # each conflict choice and each reason a search stopped.
    content += "export const GENERATION_STOP_FIELDS = " + json.dumps(described(s.GenerationOptions, ('stop_without_improvement_seconds', 'target_gap_percent', 'target_score', 'stop_by', 'on_conflict'))) + " as const;\n"
    content += "export const GENERATION_CONFLICT_CHOICES = " + json.dumps(s.GENERATION_CONFLICT_CHOICES) + " as const;\n"
    content += "export const GENERATION_STOP_REASONS = " + json.dumps(s.GENERATION_STOP_REASONS) + " as const;\n"
    content += "export const WISH_KINDS = " + json.dumps(list(s.WISH_KINDS)) + " as const;\n"
    content += "export const SWAP_KINDS = " + json.dumps(list(s.SWAP_KINDS)) + " as const;\n"
    content += "export const PATTERN_WISH_KINDS = " + json.dumps([k for k in s.WISH_KINDS if k in s.PATTERN_WISH_KINDS]) + " as const;\n"
    content += "export const MAX_WISH_POINTS = " + str(s.MAX_WISH_POINTS) + ";\n"
    content += "export const MAX_SCORE_INPUT = " + str(s.MAX_SCORE_INPUT) + ";\n"
    content += "export const SCORE_CHECK_LIMITS = " + json.dumps(s.SCORE_CHECK_LIMITS) + " as const;\n"
    content += "export const WEB_KEPT_READS = " + json.dumps(s.WEB_KEPT_READS) + " as const;\n"
    content += "export const AI_PROVIDER_KINDS = " + json.dumps(s.AI_PROVIDER_KINDS) + " as const;\n"
    content += "export const AI_REASONING_VALUES = " + json.dumps(s.AI_REASONING_VALUES) + " as const;\n"
    content += "export const WISH_RANGE_MAX_DAYS = " + str(s.WISH_RANGE_MAX_DAYS) + ";\n"
    content += "export const SYSTEM_ROLES = " + json.dumps(s.SYSTEM_ROLES) + " as const;\n"
    content += "export const EXPLICIT_ROLES = " + json.dumps(sorted(s.EXPLICIT_ROLES)) + " as const;\n"
    content += "export const ROLE_LABELS = " + json.dumps(s.ROLE_LABELS) + " as const;\n"
    content += "export const PERMISSIONS = " + json.dumps(s.PERMISSIONS) + " as const;\n"
    content += "export const CAPABILITIES = " + json.dumps(s.CAPABILITIES) + " as const;\n"
    content += "export const APP_ROUTE_PARTS = " + json.dumps(list(s.APP_ROUTE_PARTS)) + " as const;\n"
    content += "export const FEEDBACK_KINDS = " + json.dumps(list(s.FEEDBACK_KINDS)) + " as const;\n"
    content += "export const FEEDBACK_STATUSES = " + json.dumps(list(s.FEEDBACK_STATUSES)) + " as const;\n"
    content += "export const FEEDBACK_LIMITS = " + json.dumps(s.FEEDBACK_LIMITS) + " as const;\n"
    content += "export const SWAP_LIMITS = " + json.dumps(s.SWAP_LIMITS) + " as const;\n"
    content += "export const MONITOR_LIMITS = " + json.dumps(s.MONITOR_LIMITS) + " as const;\n"
    content += "export const MAX_COUNT_BUCKETS = " + str(s.MAX_COUNT_BUCKETS) + ";\n"
    content += "export const CONTENT_PAGE_SIZE = " + str(s.CONTENT_PAGE_SIZE) + ";\n"
    content += "export const CONDITION_FIELDS = " + json.dumps(list(s.CONDITION_FIELDS)) + " as const;\n"
    content += "export const CONDITION_FIELD_DEFINITIONS = " + json.dumps(s.CONDITION_FIELD_DEFINITIONS) + " as const;\n"
    content += "export const TABLE_CONDITION_DEFINITIONS = " + json.dumps(s.table_condition_registry()) + " as const;\n"
    content += "export const CONDITION_OPERATORS = " + json.dumps(s.CONDITION_OPERATORS) + " as const;\n"
    content += "export const RELATIVE_TOKEN_CATALOGUE = " + json.dumps(s.RELATIVE_TOKEN_CATALOGUE) + " as const;\n"
    content += 'export type ConditionOperand = ConditionResolveInput["operand"];\n'
    content += "export const TAG_KINDS = " + json.dumps(list(s.TAG_KINDS)) + " as const;\n"
    content += "export const REQUEST_SORT_FIELDS = " + json.dumps(list(s.REQUEST_SORT_FIELDS)) + " as const;\n"
    content += "export const REQUEST_KINDS = " + json.dumps(list(s.REQUEST_KINDS)) + " as const;\n"
    content += "export const REQUEST_STAGE_LABELS = " + json.dumps(s.REQUEST_STAGE_LABELS) + " as const;\n"
    content += "export const MAX_REQUEST_DECISIONS = " + str(s.MAX_REQUEST_DECISIONS) + " as const;\n"
    content += "export const BULK_ITEM_KINDS = " + json.dumps(s.BULK_ITEM_KINDS) + " as const;\n"
    content += "export type BulkItemKind = typeof BULK_ITEM_KINDS[number];\n"
    content += "export const TAG_LIMITS = " + json.dumps(s.TAG_LIMITS) + " as const;\n"
    content += "export const MAX_APPROVALS_REQUIRED = " + json.dumps(s.MAX_APPROVALS_REQUIRED) + ";\n"
    content += "export const BULK_LIMITS = " + json.dumps(s.BULK_LIMITS) + " as const;\n"
    content += "export const BULK_DEFAULTS = " + json.dumps(s.BulkSettings().model_dump()) + " as const;\n"
    content += "export const AUTO_SCHEDULE_LIMITS = " + json.dumps(s.AUTO_SCHEDULE_LIMITS) + " as const;\n"
    content += "export const GENERATION_CONTROL_DEFAULTS = " + json.dumps(s.GENERATION_CONTROL_DEFAULTS) + " as const;\n"
    # The run's times: their words, bounds and contract unit (a field's name ends in its unit), for the one duration input.
    times = {**described(s.GenerationOptions, ('search_minutes', 'max_search_minutes', 'stop_without_improvement_seconds',
                                                'fairness_plateau_seconds', 'diagnostic_seconds')),
             **described(s.GenerationControls, ('extension_minutes',))}
    for name, field in times.items():
        field['unit'] = 'minutes' if name.endswith('_minutes') else 'seconds'
    content += "export const GENERATION_TIME_FIELDS = " + json.dumps(times) + " as const;\n"
    for name in ('SEARCH_MODES', 'SEARCH_SCOPES', 'SEARCH_ENTITY_KINDS', 'PLANNER_LAYER_DEFAULTS', 'MEDIA_LIMITS', 'BRANDING_IMAGE_FORMATS', 'BRANDING_IMAGE_LIMITS'):
        content += f"export const {name} = " + json.dumps(getattr(s, name)) + " as const;\n"
    content += "export const SEARCH_DEFAULTS = " + json.dumps(s.SearchOptions().model_dump(mode='json')) + " as const;\n"
    content += "export type SearchEntityKind = typeof SEARCH_ENTITY_KINDS[number];\n"
    content += "export const BRANDING_DEFAULTS = " + json.dumps(s.BRANDING_DEFAULTS) + " as const;\n"
    content += "export const GENERATION_SEARCH_FINISHED_STAGES: readonly string[] = " + json.dumps(s.GENERATION_SEARCH_FINISHED_STAGES) + ";\n"
    content += "export const GENERATION_PROGRESS_STAGES = " + json.dumps(s.GENERATION_PROGRESS_STAGES) + " as const;\n"
    bulk_setting_fields = {key: {'label': field['title'], **{name: field[name]
        for name in ('description', 'minimum', 'maximum', 'default')}}
        for key, field in schemas['BulkSettings']['properties'].items()}
    content += "export const BULK_SETTING_FIELDS = " + json.dumps(bulk_setting_fields) + " as const;\n"
    # Reuse the command bus's role rules, including its catalogue-specific permissions.
    from apps.api.commands import command_roles
    from apps.api.services.bulk_selection import collection_readers
    from apps.api.services.search import registry as search_registry
    content += "export const SEARCH_COLLECTIONS = " + json.dumps(search_registry()) + " as const;\n"
    bulk_collections = s.bulk_collection_registry(command_roles, collection_readers)
    content += "export const BULK_COLLECTIONS = " + json.dumps(bulk_collections) + " as const;\n"
    content += "export type BulkCollection = keyof typeof BULK_COLLECTIONS;\n"
    content += "export const EVENT_HORIZON_MAX_DAYS = " + str(s.EVENT_HORIZON_MAX_DAYS) + " as const;\n"
    content += "export const EVENT_OCCURRENCE_RANGE_MAX_DAYS = " + str(s.EVENT_OCCURRENCE_RANGE_MAX_DAYS) + " as const;\n"
    # The condition fields a tag may stand in, and the kind of its members each reads (packages/domain/conditions.py).
    from packages.domain.conditions import TAG_FIELDS
    content += "export const TAG_FIELDS: Readonly<Record<string, string>> = " + json.dumps(TAG_FIELDS, sort_keys=True) + ";\n"
    content += "export const INDEPENDENT_ACTIONS = " + json.dumps(list(s.INDEPENDENT_ACTIONS)) + " as const;\n"
    # What a saved Schedule view can draw on a card and in a person's label, in their default order.
    content += "export const VIEW_PROPERTIES = " + json.dumps(list(s.VIEW_PROPERTIES)) + " as const;\n"
    content += "export const VIEW_PROPERTIES_HIDDEN_BY_DEFAULT = " + json.dumps(list(s.VIEW_PROPERTIES_HIDDEN_BY_DEFAULT)) + " as const;\n"
    content += "export const LABEL_PARTS = " + json.dumps(list(s.LABEL_PARTS)) + " as const;\n"
    for name in ('PLANNER_DIMENSIONS', 'PLANNER_TIME_AXES', 'VIEW_SORT_DIRECTIONS', 'PLANNER_SORT_LABELS',
                 'PLANNER_PEOPLE_SORT_LABELS', 'PLANNER_COLOUR_LABELS', 'PLANNER_PERSON_DISPLAYS',
                 'PLANNER_PERSON_DISPLAY_DEFAULT', 'PLANNER_TEAM_ROWS_DEFAULT', 'PLANNER_TEAM_ROWS_LIMIT', 'PLANNER_WIDTH_KEYS', 'VIEW_PROPERTY_DEFINITIONS', 'VIEW_ITEM_SHOWN_BY_DEFAULT', 'TABLE_GROUP_FIELDS',
                 'LABEL_PART_DEFINITIONS', 'LABEL_SEPARATORS', 'LABEL_HONORIFIC_MODES', 'HONORIFIC_DEFAULTS',
                 'NAME_PARTICLE_DEFAULTS', 'NAME_LABEL_DEFAULTS', 'PERSON_PROPERTY_DEFINITIONS',
                 'PERSON_PROPERTY_ALIASES', 'FAIRNESS_METRIC_LABELS',
                 'PLANNER_COLOUR_TAG_LIMIT', 'PRINT_IMAGE_DPI', 'PRINT_IMAGE_DPI_DEFAULT', 'PRINT_WRAP_DEFAULT',
                 'WHEREABOUTS_DEFAULTS', 'WHEREABOUTS_LIMITS'):
        content += f"export const {name} = " + json.dumps(getattr(s, name)) + " as const;\n"
    content += "export const ENDED_REASON = " + json.dumps(s.ENDED_REASON) + ";\n"
    # Which rule kinds take lines, and the numbers a line may set, first the usual one.
    content += "export const PER_PERSON_KINDS: readonly string[] = " + json.dumps(sorted(s.PER_PERSON_KINDS)) + ";\n"
    content += "export const LINE_KINDS: readonly string[] = " + json.dumps(sorted(s.LINE_KINDS)) + ";\n"
    content += "export const NEVER_OVERRIDDEN: readonly string[] = " + json.dumps(sorted(s.NEVER_OVERRIDDEN)) + ";\n"
    content += "export const LINE_NUMBER_FIELDS: Readonly<Record<string, readonly string[]>> = " + json.dumps(s.LINE_NUMBER_FIELDS, sort_keys=True) + ";\n"
    content += "export const LINE_SETTINGS: Readonly<Record<string, readonly string[]>> = " + json.dumps(s.LINE_SETTINGS, sort_keys=True) + ";\n"
    content += "export const ANY_LINE_SETTINGS: readonly string[] = " + json.dumps(sorted(s.ANY_LINE_SETTINGS)) + ";\n"
    # The one number each rule kind has: [field, unit, which way is stricter] (packages/domain/rule_map.py NUMBERS).
    from packages.domain.rule_map import NUMBERS
    content += ("export const RULE_NUMBERS: Readonly<Record<string, readonly [string, string | null, string | null]>> = "
                + json.dumps(NUMBERS, sort_keys=True) + ";\n")
    # The rule catalogue: one entry per idea, its numbers' slots and settings.
    from packages.contracts.rule_catalogue import (CATALOGUE_MAPPING, CATALOGUE_VERSION, FAMILIES,
                                                   QUESTION_GROUP_ORDER, RULE_CATALOGUE, RULE_CATALOGUE_V2)
    content += "export const RULE_FAMILIES: readonly (readonly [string, string])[] = " + json.dumps(FAMILIES) + ";\n"
    content += "export const RULE_CATALOGUE: readonly RuleCatalogueEntry[] = " + json.dumps(RULE_CATALOGUE) + ";\n"
    content += "export const RULE_CATALOGUE_V2: readonly RuleCatalogueEntry[] = " + json.dumps(RULE_CATALOGUE_V2) + ";\n"
    content += "export const CATALOGUE_VERSION = " + str(CATALOGUE_VERSION) + " as const;\n"
    content += "export const CATALOGUE_MAPPING: Readonly<Record<string, string | null>> = " + json.dumps(CATALOGUE_MAPPING) + ";\n"
    content += "export const QUESTION_GROUP_ORDER: readonly string[] = " + json.dumps(QUESTION_GROUP_ORDER) + ";\n"
    content += ("export type RuleCatalogueEntry = {id: string; family: string; name: string; description: string; unit: string | null;\n"
                "fix_template: {sentence: string; meta: string; [variant: string]: string};\n"
                "  numbers: Record<string, {kind: string; field: string | null; fixed: Record<string, unknown>; advisory: boolean}[]>;\n"
                "  levels: string[]; default_line: Partial<RuleLine>; default_settings: Record<string, Record<string, unknown>>;\n"
                "  suggested_lines?: Record<string, {default_line: Partial<RuleLine>; default_settings: Record<string, Record<string, unknown>>}>;\n"
                "  safety: boolean; count_sides: ('who' | 'where')[]; line_settings: Record<string, string[]>; needs: Record<string, string[]>;\n"
                "  catalogue_version: number; authorable: boolean; question_group?: string; replacement_id?: string | null; replaces?: string[]; mixed_safety?: boolean;\n"
                "  question_group_aliases?: Record<string, string>; question_group_by_scope?: Record<string, string>; question_group_by_slot?: Record<string, string>; question_group_by_slot_level?: Record<string, Record<string, string>>;\n"
                "  measures?: string[]; periods?: string[]; modes?: string[]; setting_labels?: Record<string, string>; setting_help?: Record<string, string>;\n"
                "  setting_option_labels?: Record<string, Record<string, string>>};\n")
    return content, {cls.__name__: cls.model_json_schema() for cls in CLASSES}


def main():
    content, schemas = render()
    target = ROOT / "apps/web/src/generated.ts"
    contract_file = ROOT / "packages/contracts/public-schemas.json"
    if "--check" in sys.argv:
        if not target.exists() or target.read_text() != content or json.loads(contract_file.read_text()) != schemas:
            raise SystemExit("Generated contracts are out of date")
        print("Generated contracts match server schemas")
    else:
        target.write_text(content)
        contract_file.write_text(json.dumps(schemas, indent=2) + "\n")
        print("Generated TypeScript and JSON schemas")


if __name__ == "__main__": main()
