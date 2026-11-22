"""Public audit wording and filter groups, shared by the API and generated client contracts."""
AUDIT_GROUPS = {'rotas': 'Rotas & sessions', 'config': 'Configuration', 'rules': 'Rules',
    'people': 'Requests & swaps', 'access': 'Access', 'system': 'System'}

# Prefix: singular, plural, group, colour, supported actions. Wording is independent of private payloads.
_KINDS = {
    'workspace': ('rota', 'rotas', 'rotas', 'violet', 'create submit approve publish validate undo redo reopen archive restore delete_draft archive_due designate merge.apply merge.preview baseline.capture baseline.refresh.preview baseline.refresh.apply share rule_adjust target_override resolve_overlap'),
    'shift': ('session', 'sessions', 'rotas', 'teal', 'create update action copy undo_batch'),
    'publication': ('publication', 'publications', 'rotas', 'violet', 'publish change change.undo changes.prepare changes.submit changes.withdraw'),
    'job': ('check', 'checks', 'rotas', 'violet', 'create cancel add_time continue stop_keep_best dismiss attach release'),
    'generation': ('auto-schedule result', 'auto-schedule results', 'rotas', 'violet', 'apply'),
    'scheduling': ('auto-schedule run', 'auto-schedule runs', 'rotas', 'violet', 'create'),
    'series': ('recurring session', 'recurring sessions', 'rotas', 'teal', 'expand reconcile occurrence.cancel'),
    'catalogue': ('setting', 'settings', 'config', 'amber', 'create update bulk_update undo_create'),
    'department': ('department setting', 'department settings', 'config', 'amber', 'create settings display display.restore bulk_settings sessions'),
    'score': ('score setting', 'score settings', 'config', 'amber', 'settings settings.rollback'),
    'branding': ('application branding', 'application branding', 'config', 'amber', 'update undo image_upload'),
    'membership': ('rank', 'ranks', 'config', 'amber', 'create end update_dates undo_create bulk_create withdraw restore'),
    'programme_membership': ('programme membership', 'programme memberships', 'config', 'amber', 'withdraw restore'),
    'eligibility': ('eligibility', 'eligibilities', 'config', 'amber', 'create end extend update_dates undo_create bulk_create programme withdraw restore'),
    'grant': ('capability', 'capabilities', 'config', 'amber', 'create revoke restore update_dates undo_create bulk_create'),
    'closure': ('closure', 'closures', 'config', 'amber', 'create'),
    'tag': ('tag', 'tags', 'config', 'amber', 'members member_add member_remove'),
    'date_set': ('date set', 'date sets', 'config', 'amber', 'adjust copy_next_year delete import entry.create entry.update entry.delete'),
    'staff_import': ('staff import', 'staff imports', 'config', 'amber', 'preview apply undo'),
    'import': ('import', 'imports', 'config', 'amber', 'preview apply refresh reference_preview'),
    'reference': ('import', 'imports', 'config', 'amber', 'create'),
    'score_import': ('rota import', 'rota imports', 'rotas', 'teal',
        'upload add_files update_source remove_source restore_source time profile discard restore check decide save'),
    'score_check': ('rota comparison', 'rota comparisons', 'rotas', 'violet',
        'upload update check decide undo save auto benchmark'),
    'ai_provider': ('AI provider', 'AI providers', 'config', 'amber', 'enabled save key delete restore test'),
    'policy': ('rule', 'rules', 'rules', 'indigo', 'create activate activate_many decline retire retire_pending restore restore_pending restore_proposal group withdraw_approval'),
    'rule': ('rule', 'rules', 'rules', 'indigo', 'acknowledge unacknowledge migrate_to_catalogue catalogue.check propose'),
    'exception': ('rule exception', 'rule exceptions', 'rules', 'indigo', 'create create_many approve end undo'),
    'availability': ('absence or leave record', 'absence or leave records', 'people', 'blue', 'record retract restore update undo_update undo_create'),
    'availability_pattern': ('weekly availability', 'weekly availability', 'people', 'blue', 'propose decide end restore undo'),
    'wish': ('wish', 'wishes', 'people', 'blue', 'create update withdraw restore override'),
    'request': ('request', 'requests', 'people', 'blue', 'create decide decide_many withdraw undo link_rota'),
    'swap': ('swap', 'swaps', 'people', 'pink', 'offer counteroffer accept confirm reject withdraw prepare undo'),
    'person': ('person record', 'person records', 'people', 'blue', 'anonymise overtime workload contact.set'),
    'rotation': ('rotation', 'rotations', 'people', 'blue', 'plan copy remove restore request_change swap undo_plan'),
    'timeline': ('rotation', 'rotations', 'people', 'blue', 'rotation'),
    'trainee_note': ('trainee note', 'trainee notes', 'people', 'blue', 'create update delete restore'),
    'role': ('role', 'roles', 'access', 'slate', 'grant revoke restore'),
    'identity': ('sign-in identity', 'sign-in identities', 'access', 'slate', 'bind disable enable'),
    'account': ('account', 'accounts', 'access', 'slate', 'invite resend reset_link revoke_invite end_sessions end_other_sessions invite_accepted password_reset password_change first_sign_in'),
    'self_approval': ('self-approval permission', 'self-approval permissions', 'access', 'slate', 'grant revoke restore'),
    'review_route': ('review route', 'review routes', 'access', 'slate', 'create update activate retire'),
    'review_delegation': ('review delegation', 'review delegations', 'access', 'slate', 'create revoke restore'),
    'session': ('device', 'devices', 'access', 'slate', 'revoke logout'),
    'auth': ('device', 'devices', 'access', 'slate', 'sign_in sign_out session_revoke'),
    'auth.config': ('sign-in setting', 'sign-in settings', 'access', 'slate', 'update undo reset anonymise'),
    'calendar_feed': ('calendar link', 'calendar links', 'access', 'slate', 'create revoke settings'),
    'table_view': ('saved view', 'saved views', 'system', 'slate', 'create update delete set_default'),
    'planner_view': ('saved view', 'saved views', 'system', 'slate', 'create update delete'),
    'notification': ('notification', 'notifications', 'system', 'slate', 'ack ack_all unack acknowledge policy.update policy.undo preferences.update preferences.undo'),
    'feedback': ('feedback report', 'feedback reports', 'system', 'slate', 'create update undo export_github screenshot.read'),
    'monitor': ('monitor token', 'monitor tokens', 'system', 'slate', 'token_changed'),
    'integration': ('GitHub integration', 'GitHub integrations', 'system', 'slate', 'github.connect github.disconnect github.test'),
    'email': ('email setting', 'email settings', 'system', 'slate', 'settings microsoft.connect microsoft.disconnect'),
    'event': ('event', 'events', 'people', 'blue', 'occurrence.update rsvp'),
    'event_types': ('event type', 'event types', 'config', 'amber', 'import_defaults'),
    'department_content': ('notice', 'notices', 'people', 'blue', 'seen remind'),
    'bulk': ('bulk change', 'bulk changes', 'system', 'slate', 'apply stage staged.drop staged.expire'),
    'issues': ('rota issue', 'rota issues', 'rotas', 'violet', 'resolve'),
    'outbox': ('notification', 'notifications', 'system', 'slate', 'retry'),
    'draft': ('rota', 'rotas', 'rotas', 'violet', 'swap move'),
    'planning': ('planning setting', 'planning settings', 'config', 'amber', 'settings settings.rollback'),
    'planning_group': ('planning group', 'planning groups', 'config', 'amber', 'create update rollback'),
    'absence_report': ('absence report', 'absence reports', 'people', 'blue', 'settings'),
    'push': ('push subscription', 'push subscriptions', 'access', 'slate', 'subscribe unsubscribe rotate test'),
    'pwa': ('PWA setting', 'PWA settings', 'system', 'slate', 'provider.resume system.update'),
}
_VERBS = {
    'create': 'added', 'create_many': 'added', 'bulk_create': 'added', 'update': 'changed', 'bulk_update': 'changed',
    'update_dates': 'changed the dates of', 'publish': 'published', 'approve': 'approved', 'submit': 'submitted',
    'validate': 'checked', 'capture': 'captured', 'reject': 'returned', 'retire': 'retired', 'activate': 'activated', 'activate_many': 'activated',
    'propose': 'proposed', 'revoke': 'revoked', 'grant': 'granted', 'disable': 'disabled', 'enable': 'enabled', 'bind': 'bound',
    'end': 'ended', 'record': 'recorded', 'cancel': 'closed', 'decide': 'decided', 'decide_many': 'decided', 'action': 'updated',
    'settings': 'updated', 'bulk_settings': 'updated', 'display': 'updated', 'offer': 'offered', 'counteroffer': 'offered a duty in return for',
    'accept': 'accepted', 'confirm': 'confirmed', 'withdraw': 'withdrew', 'prepare': 'prepared a rota change for',
    'rule_adjust': 'changed a rule for', 'target_override': 'changed a target for', 'decline': 'declined', 'delete': 'deleted',
    'undo': 'undid a change in', 'redo': 'redid a change in', 'undo_update': 'undid a change in', 'undo_create': 'undid adding', 'undo_batch': 'undid changes to',
    'apply': 'applied', 'link_rota': 'linked a rota to', 'ack': 'read', 'ack_all': 'read all', 'unack': 'undid reading', 'acknowledge': 'acknowledged',
    'unacknowledge': 'undid acknowledgement of', 'seen': 'read', 'remind': 'sent reminders for', 'expand': 'added sessions from',
    'reconcile': 'updated', 'sign_in': 'signed in on', 'sign_out': 'signed out on', 'session_revoke': 'ended',
    'members': 'changed the members of', 'member_add': 'added a member to', 'member_remove': 'removed a member from',
    'restore': 'restored', 'restore_proposal': 'restored the proposal for', 'reopen': 'reopened', 'archive': 'archived', 'delete_draft': 'deleted',
    'retire_pending': 'approved a pending retirement of', 'restore_pending': 'approved a pending restoration of',
    'withdraw_approval': 'withdrew an approval to retire or restore',
    'reset': 'reset', 'image_upload': 'uploaded an image for',
    'resolve_overlap': 'resolved overlapping', 'copy': 'copied', 'copy_next_year': 'copied to next year',
    'adjust': 'adjusted', 'import': 'imported', 'preview': 'previewed', 'reference_preview': 'previewed', 'refresh': 'refreshed',
    'extend': 'extended', 'retract': 'retracted', 'override': 'overrode', 'anonymise': 'anonymised',
    'set': 'changed contact details for', 'overtime': 'changed overtime for', 'workload': 'changed workload for', 'plan': 'planned', 'remove': 'removed',
    'request_change': 'requested a change to', 'swap': 'swapped', 'undo_plan': 'undid planning', 'rotation': 'changed',
    'invite': 'invited', 'resend': 'resent an invitation for', 'reset_link': 'sent a password reset for',
    'revoke_invite': 'revoked an invitation for', 'end_sessions': 'ended sessions for', 'end_other_sessions': 'ended other sessions for',
    'invite_accepted': 'accepted their invitation', 'password_reset': 'reset the password for',
    'password_change': 'changed the password for', 'first_sign_in': 'first signed in to',
    'dismiss': 'dismissed', 'attach': 'attached', 'release': 'released', 'move': 'moved',
    'add_time': 'added time to', 'continue': 'continued', 'stop_keep_best': 'stopped and kept the best result from',
    'migrate_to_catalogue': 'converted to the catalogue', 'check': 'checked', 'export_github': 'exported to GitHub',
    'read': 'read', 'token_changed': 'changed', 'connect': 'connected', 'disconnect': 'disconnected', 'test': 'tested',
    'rsvp': 'responded to', 'import_defaults': 'imported defaults for', 'stage': 'staged', 'drop': 'dropped',
    'expire': 'expired', 'resolve': 'resolved', 'retry': 'retried', 'sessions': 'changed session settings for',
    'programme': 'changed programme eligibility for', 'group': 'grouped', 'logout': 'signed out on',
    'set_default': 'changed the default for',
    'upload': 'uploaded', 'add_files': 'added files to', 'update_source': 'changed a file in',
    'remove_source': 'removed a file from', 'restore_source': 'restored a file in',
    'time': 'changed time for', 'profile': 'changed the provider for', 'discard': 'discarded', 'save': 'saved',
    'auto': 'started auto-scheduling for', 'benchmark': 'compared',
    'enabled': 'changed AI availability for', 'key': 'replaced the key for',
    'rollback': 'rolled back', 'share': 'shared', 'designate': 'designated', 'archive_due': 'archived',
    'merge': 'merged', 'change': 'changed', 'changes': 'changed', 'submit': 'submitted',
    'withdraw': 'withdrew', 'provider': 'changed', 'system': 'updated', 'subscribe': 'subscribed',
    'unsubscribe': 'unsubscribed', 'rotate': 'rotated', 'resume': 'resumed',
}
AUDIT_ACTION_CATALOGUE = {
    f'{head}.{action}': {'noun': noun, 'plural': plural, 'group': group, 'colour': colour,
        'verb': 'started' if head == 'job' and action == 'create' else _VERBS[action.split('.')[-1]]}
    for head, (noun, plural, group, colour, actions) in _KINDS.items() for action in actions.split()
}
AUDIT_ACTION_CATALOGUE['department.score_check_refused'] = {
    'noun': 'score-check enablement', 'plural': 'score-check enablement',
    'group': 'access', 'colour': 'slate', 'verb': 'was refused',
}
AUDIT_ACTION_CATALOGUE['command.refused'] = {
    'noun': 'Refused changes', 'plural': 'Refused changes',
    'group': 'access', 'colour': 'slate', 'verb': 'was refused a change',
}


def audit_summary(action, count=1, subject=None):
    """The one sentence an audit entry shows after its actor (screen, search and CSV): "added a rank: <subject>"."""
    entry = AUDIT_ACTION_CATALOGUE.get(action)
    if entry is None:
        words = action.replace('.', ' ').replace('_', ' ')
    elif action == 'command.refused':
        words = entry['verb'] if count == 1 else f"was refused {count} changes"
    elif count > 1:
        words = f"{entry['verb']} {count} {entry['plural']}"
    else:
        words = f"{entry['verb']} {'an' if entry['noun'][:1].lower() in 'aeiou' else 'a'} {entry['noun']}"
    return words + (f": {subject}" if subject else '')


def summary_actions(text):
    """Actions whose own sentence contains this text ("rank" finds "added a rank" and "ended a rank")."""
    needle = (text or '').strip().lower()
    return sorted(action for action in AUDIT_ACTION_CATALOGUE if needle and needle in audit_summary(action).lower())
