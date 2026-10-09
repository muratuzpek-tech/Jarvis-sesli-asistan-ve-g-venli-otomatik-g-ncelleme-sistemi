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

def test_smart_type_uses_wayland_backend(monkeypatch):
    calls = []

    class FakeGUI:
        @staticmethod
        def typewrite(text, interval=0.0):
            calls.append(("typewrite", text, interval))

    monkeypatch.setattr(module, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(module, "backend_name", lambda: "wayland-ydotool")
    monkeypatch.setattr(module, "pyautogui", FakeGUI())
    monkeypatch.setattr(module, "_clear_field", lambda: "cleared")

    result = module._smart_type("Türkçe karakterler: ığüşöç İĞÜŞÖÇ", clear_first=False)

    assert calls == [
        ("typewrite", "Türkçe karakterler: ığüşöç İĞÜŞÖÇ", 0.04)
    ]
    assert result.startswith("Smart-typed (desktop backend):")


def test_clipboard_paste_uses_wayland_backend(monkeypatch):
    calls = []

    class FakeGUI:
        @staticmethod
        def typewrite(text, interval=0.0):
            calls.append(("typewrite", text, interval))

    monkeypatch.setattr(module, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(module, "backend_name", lambda: "wayland-ydotool")
    monkeypatch.setattr(module, "pyautogui", FakeGUI())

    result = module._clipboard_paste("Merhaba, dünya! Şişli'de ığdır.")

    assert calls == [
        ("typewrite", "Merhaba, dünya! Şişli'de ığdır.", 0.0)
    ]
    assert result.startswith("Pasted (desktop backend):")
