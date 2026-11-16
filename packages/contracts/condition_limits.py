"""Shared admission/runtime budget for Count-each expansion, before allocating bucket sets."""

MAX_COUNT_BUCKETS = 500


def count_bucket_bound(tree):
    """Conservative peak expansion size, saturating at limit + 1; no products or sets are built."""
    from packages.domain.conditions import absolute_values
    cap = MAX_COUNT_BUCKETS + 1

    def visit(node):
        if not node:
            return 0, 0, False
        each = node.get('count') == 'each'
        if 'conditions' not in node:
            each = each and node.get('op') != 'none'
            width = min(cap, len(absolute_values(node) or [])) if each else 1
            return width, width, each
        children = node.get('conditions') or []
        conjunction = node.get('match', 'all') == 'all'
        width, peak = (1 if conjunction and children else 0), 0
        for child in children:
            size, child_peak, child_each = visit(child)
            width = min(cap, width * size if conjunction else width + size)
            peak = max(peak, child_peak)
            each = each or child_each
        if conjunction and node.get('count') == 'each':
            width = min(cap, width * len(children))
        return width, max(peak, width), each

    _, peak, each = visit(tree)
    return peak if each else 1


def check_count_bucket_budget(tree, line='Condition line'):
    count = count_bucket_bound(tree)
    if count > MAX_COUNT_BUCKETS:
        raise ValueError(f'{line}: Count each expansion bound is at least {count} bucket entries; '
                         f'the limit is {MAX_COUNT_BUCKETS}. Select fewer values or use Count together.')
