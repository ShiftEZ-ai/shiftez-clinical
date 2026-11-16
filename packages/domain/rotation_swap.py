"""Exchange rotation dates while retaining each rotation's training and attachment facts."""


def swapped_dates(first, second):
    """Return new values for both rotations; inputs and their non-date facts stay unchanged."""
    dates = ("starts_on", "ends_on")
    return ({**first, **{key: second[key] for key in dates}},
            {**second, **{key: first[key] for key in dates}})
