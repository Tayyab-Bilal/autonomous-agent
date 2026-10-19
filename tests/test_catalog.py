from autonomous_agent.assembler import EVENT_MAP, load_catalog


def test_every_mapped_trigger_exists_in_the_catalog():
    types = {t["type"] for t in load_catalog()}
    assert set(EVENT_MAP.values()) <= types


def test_every_catalog_type_is_mapped_or_explicitly_excluded():
    mapped = set(EVENT_MAP.values())
    for entry in load_catalog():
        assert entry["type"] in mapped or entry.get("excluded_reason"), \
            f"{entry['type']} is neither in EVENT_MAP nor excluded with a reason"


def test_excluded_types_are_not_mapped():
    excluded = {t["type"] for t in load_catalog() if t.get("excluded_reason")}
    assert excluded and not excluded & set(EVENT_MAP.values())


def test_catalog_has_no_duplicates():
    types = [t["type"] for t in load_catalog()]
    assert len(types) == len(set(types)) >= 20
