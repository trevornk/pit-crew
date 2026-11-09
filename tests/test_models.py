from pit_crew.brain.models import FIELD_NAMES, FieldValue, VehicleState, describe_state


def make_state(now=1000.0, **vals):
    st = VehicleState(name="Test", source="test", captured_at=now)
    for k, v in vals.items():
        st.set(k, v, now)
    return st


def test_all_fields_default_unavailable():
    st = VehicleState(name="x", source="t", captured_at=1.0)
    assert set(st.fields) == set(FIELD_NAMES)
    assert not st.has("battery_pct")
    assert st.is_available("battery_pct") is False


def test_value_freshness_and_staleness():
    st = make_state(now=1000.0, battery_pct=71)
    st.fields["range_mi"] = FieldValue(200.0, 100.0)
    assert st.age_s("battery_pct", 1060.0) == 60
    assert st.stale_fields(1060.0, 900) == ["range_mi"]
    assert st.age_s("locked", 1000.0) is None


def test_public_has_no_coordinates_and_lists_unavailable():
    st = make_state(battery_pct=50, location_zone="home", locked=True)
    pub = st.to_public(now=1010.0)
    blob = str(pub).lower()
    assert "latitude" not in blob and "longitude" not in blob
    assert pub["location"] == {"location_zone": "home"}
    assert "climate_on" in pub["freshness"]["unavailable_fields"]
    assert pub["freshness"]["newest_update_age_s"] == 10 and pub["freshness"]["stale_fields"] == {}


def test_motion_fallbacks():
    assert make_state(moving=True).motion() is True
    assert make_state(speed_mph=0.0).motion() is False
    assert make_state(shift_state="D").motion() is True
    assert make_state(shift_state="P").motion() is False
    assert make_state(plugged_in=True).motion() is False
    assert make_state().motion() is None


def test_storage_roundtrip():
    st = make_state(battery_pct=33, doors_open=["rear_left"])
    again = VehicleState.from_storage(st.to_storage())
    assert again.value("battery_pct") == 33 and again.value("doors_open") == ["rear_left"]


def test_describe_state_us_units():
    st = make_state(battery_pct=80, range_mi=222.9, locked=False, location_zone="home", cabin_temp_f=70)
    text = describe_state(st, 1000.0)
    assert "80% battery" in text and "223 mi" in text and "UNLOCKED" in text and "70°F" in text
