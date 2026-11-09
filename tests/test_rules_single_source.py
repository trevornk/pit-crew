"""Alert-rule edits live in the plugin's own `rules` setting (one source of truth); old DB overrides migrate once."""

from pit_crew.watcher.rules import RULES_BY_ID


class SetCfgCtx:
    """A PluginContext stand-in whose set_config records the write (what Hermes persists as the setting)."""

    def __init__(self, inner):
        self.inner, self.writes = inner, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def set_config(self, key, value):
        self.writes.append((key, value))


def test_set_rule_writes_the_plugin_setting_not_the_db(stub_service, stub):
    svc = stub_service()
    svc.ctx = SetCfgCtx(svc.ctx)
    svc.engine  # noqa: B018
    svc.watcher.engine.set_rule("left_unlocked_away", minutes=20, enabled=False)
    assert ("rules.left_unlocked_away.minutes", 20) in svc.ctx.writes and ("rules.left_unlocked_away.enabled", False) in svc.ctx.writes
    assert svc.store.kv_get("rule_overrides", None) is None


def test_edit_without_ctx_is_visible_through_the_setting_and_in_describe(stub_service, stub):
    svc = stub_service()
    svc.watcher.engine.set_rule("left_unlocked_away", minutes=33)
    assert svc.settings.get("rules")["left_unlocked_away"]["minutes"] == 33          # what Settings would show
    cfg = svc.watcher.engine.rule_config(RULES_BY_ID["left_unlocked_away"])
    assert cfg["minutes"] == 33


def test_old_db_overrides_migrate_once_then_are_dropped(stub_service, stub):
    svc = stub_service()
    svc.ctx = SetCfgCtx(svc.ctx)
    svc.store.kv_set("rule_overrides", {"left_unlocked_away": {"minutes": 45}, "nonsense": {"x": 1}, "tire_low": {"bogus": 1}})
    cfg = svc.watcher.engine.rule_config(RULES_BY_ID["left_unlocked_away"])
    assert svc.ctx.writes == [("rules.left_unlocked_away.minutes", 45)]
    assert svc.store.kv_get("rule_overrides", None) is None
    svc.ctx.writes.clear()
    svc.watcher.engine.rule_config(RULES_BY_ID["left_unlocked_away"])
    assert svc.ctx.writes == []                                                      # one time only


def test_failed_migration_keeps_honouring_the_old_overrides(stub_service, stub):
    svc = stub_service()

    class Refusing:
        def __getattr__(self, name):
            return getattr(svc_ctx, name)

        def set_config(self, key, value):
            raise PermissionError("Plugin settings cannot be changed in a managed install")

    svc_ctx = svc.ctx
    svc.ctx = Refusing()
    svc.store.kv_set("rule_overrides", {"left_unlocked_away": {"minutes": 45}})
    assert svc.watcher.engine.rule_config(RULES_BY_ID["left_unlocked_away"])["minutes"] == 45
    assert svc.store.kv_get("rule_overrides", None)                                   # kept for a later attempt
