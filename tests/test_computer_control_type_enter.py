from jarvis.actions import computer_control as module


def test_type_text_can_press_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "_type", lambda text: calls.append(("type", text)) or "Typed")
    monkeypatch.setattr(module, "_press", lambda key: calls.append(("press", key)) or "Pressed")

    result = module.computer_control(
        {"action": "type_text", "text": "echo JARVIS_VISIBLE_TEST", "press_enter": True}
    )

    assert calls == [("type", "echo JARVIS_VISIBLE_TEST"), ("press", "enter")]
    assert result == "Typed + Enter"
