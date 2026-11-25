"""Shared notification audience definitions.

The definitions are protocol data, not UI copy owned by a client.  Delivery code
uses the same reason names when it computes a recipient set.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationAudienceDefinition:
    description: str


NOTIFICATION_AUDIENCES = {
    'duty_changed': NotificationAudienceDefinition('The affected person'),
    'absence_reported': NotificationAudienceDefinition('People with the Coordinator role'),
    'publication': NotificationAudienceDefinition(
        'People on the published rota (only changed duties for amendments), active coordinators and approvers, and people with a wish in the period',
    ),
    'review': NotificationAudienceDefinition('Current reviewers and people with the Coordinator role'),
    'swap': NotificationAudienceDefinition('The people involved and current reviewers'),
    'request': NotificationAudienceDefinition('The requester and current decision makers'),
    'availability': NotificationAudienceDefinition('The affected person and people with the Coordinator role'),
    'rule': NotificationAudienceDefinition('Rule coordinators and approvers'),
    'announcement': NotificationAudienceDefinition('The announcement audience'),
    'event': NotificationAudienceDefinition('The event audience'),
    'feedback': NotificationAudienceDefinition('The reporter and authorised operators'),
    'generation_finished': NotificationAudienceDefinition('The person who started the auto-schedule run'),
    'credential_impact': NotificationAudienceDefinition('The affected person and authorised reviewers'),
    'bulk_review': NotificationAudienceDefinition('Independent reviewers'),
    'self_approval': NotificationAudienceDefinition('Authorised governance recipients'),
}


def audience_definition(kind: str) -> NotificationAudienceDefinition:
    return NOTIFICATION_AUDIENCES[kind]
