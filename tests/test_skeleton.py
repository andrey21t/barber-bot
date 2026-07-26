def test_skeleton_imports() -> None:
    from bot.config import Settings, get_settings
    from bot.handlers.start import router

    assert Settings is not None
    assert callable(get_settings)
    assert router.name == "start"
