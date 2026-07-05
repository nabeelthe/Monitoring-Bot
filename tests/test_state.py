import json

from nuva_bot.state import State, SEEN_CAP


def test_new_ids_dedupe(tmp_path):
    st = State(tmp_path / "s.json")
    assert st.new_ids("ns", ["a", "b"]) == ["a", "b"]
    assert st.new_ids("ns", ["b", "c"]) == ["c"]
    assert st.is_seen("ns", "a")
    assert not st.is_seen("other", "a")


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    st = State(path)
    st.new_ids("ns", ["x"])
    st.add_chat(42, "me")
    st.kv_set("k", {"nested": 1})
    st.save()
    st2 = State(path)
    assert st2.is_seen("ns", "x")
    assert st2.chats == [{"id": 42, "title": "me"}]
    assert st2.kv_get("k") == {"nested": 1}


def test_seen_cap(tmp_path):
    st = State(tmp_path / "s.json")
    st.new_ids("ns", [str(i) for i in range(SEEN_CAP + 100)])
    st.save()
    data = json.loads((tmp_path / "s.json").read_text())
    assert len(data["seen"]["ns"]) == SEEN_CAP
    assert st.is_seen("ns", str(SEEN_CAP + 99))  # newest kept
    assert not st.is_seen("ns", "0")             # oldest evicted


def test_corrupt_state_recovers(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json!!")
    st = State(path)
    assert st.chats == []
    assert (tmp_path / "s.corrupt").exists()


def test_mute(tmp_path):
    st = State(tmp_path / "s.json")
    assert not st.is_muted()
    st.set_mute(5)
    assert st.is_muted()
    st.set_mute(0)
    assert not st.is_muted()


def test_add_remove_chat(tmp_path):
    st = State(tmp_path / "s.json")
    assert st.add_chat(1, "a")
    assert not st.add_chat(1, "a")
    assert st.remove_chat(1)
    assert not st.remove_chat(1)
