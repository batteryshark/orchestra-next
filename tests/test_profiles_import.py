import json
import sqlite3

from orchestra import dsh, profiles
from tests.common import StateCase

CATALOG = {("deepseek-official", "deepseek-v4-flash"): {"high", "low", "max", "off"},
           ("claude-subscription", "opus"): set(), ("zai", "glm-5.3"): {"high", "low", "medium"}}


class ProfilesImportTest(StateCase):
    def v2(self):
        path = self.root / "v2.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE runtimes(runtime_id TEXT, slug TEXT);
            CREATE TABLE profiles(profile_id TEXT, slug TEXT, name TEXT, runtime_id TEXT, model TEXT, effort TEXT,
              tier INT, priority INT, max_concurrency INT, config_json TEXT, enabled INT, archived INT);
            INSERT INTO runtimes VALUES('r1','reasonix'),('r2','claude'),('r3','codex'),('r4','opencode');
            INSERT INTO profiles VALUES
              ('p1','ds-flash','ds-flash','r1','deepseek/deepseek-v4-flash','high',1,10,NULL,'{"role":"workhorse","spawn_profiles":["ds-flash"]}',1,0),
              ('p2','opus-high','opus-high','r2','claude-opus-5','high',3,10,NULL,'{}',1,0),
              ('p3','sol-high','sol-high','r3','gpt-5.6-sol','high',2,60,NULL,'{}',1,0),
              ('p4','glm-max','glm-max','r4','zai/glm-5.3',NULL,3,30,2,'{"variant":"max"}',1,0),
              ('p5','old','old','r1','deepseek/deepseek-v4-flash','low',1,0,NULL,'{}',0,0),
              ('p6','gone','gone','r1','deepseek/deepseek-v4-flash','low',1,0,NULL,'{}',1,1);
        """)
        con.commit(); con.close()
        return path

    def test_plan_then_apply(self):
        path = self.v2()
        plan = profiles.import_v2(self.con, path, catalog=CATALOG)
        by_slug = {row["slug"]: row for row in plan}
        self.assertEqual(set(by_slug), {"ds-flash", "opus-high", "sol-high", "glm-max", "old"})
        self.assertEqual(by_slug["sol-high"]["reason"], "no DSH route")
        self.assertEqual(by_slug["old"]["reason"], "disabled in V2")
        self.assertIsNone(by_slug["opus-high"]["effort"])          # claude-subscription has no efforts
        self.assertEqual(by_slug["glm-max"]["effort"], "high")     # variant max -> nearest advertised
        self.assertEqual(profiles.all_profiles(self.con), [])
        applied = profiles.import_v2(self.con, path, catalog=CATALOG, apply=True)
        self.assertEqual([r["outcome"] for r in applied if r["slug"] == "ds-flash"], ["created"])
        row = profiles.find(self.con, "ds-flash")
        self.assertEqual((row["provider"], row["model"], row["effort"], row["tier"]), ("deepseek-official", "deepseek-v4-flash", "high", 1))
        self.assertIn("workhorse; spawn: ds-flash; imported from V2", row["note"])
        self.assertEqual(profiles.find(self.con, "glm-max")["max_concurrency"], 2)
        again = profiles.import_v2(self.con, path, catalog=CATALOG, apply=True)
        self.assertEqual({r["outcome"] for r in again if r["slug"] in ("ds-flash", "glm-max")}, {"exists"})

    def test_provider_keys_from_opencode_store(self):
        store = self.root / "auth.json"
        store.write_text(json.dumps({"meta": {"type": "api", "key": "m-key"}, "zai": {"type": "api", "key": ""}, "xai": {"type": "oauth"}}))
        codex = self.root / "codex.json"
        codex.write_text(json.dumps({"tokens": {"access_token": "jwt", "refresh_token": "r"}}))
        self.assertEqual(dsh.provider_keys(store, self.root / "missing.json"), {"META_API_KEY": "m-key"})
        self.assertEqual(dsh.provider_keys(store, codex), {"META_API_KEY": "m-key", "OPENAI_CODEX_API_KEY": "jwt"})
        self.assertEqual(dsh.provider_keys(self.root / "missing.json", self.root / "missing.json"), {})
