# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Registry tests — TOML target registry, auth-by-reference, deny-biased resolution."""
import unittest

from pacioli.registry import RegistryError, load_registry, resolve_auth


def _load(toml_text):
    return load_registry(toml_text=toml_text)


VALID = """
[targets.prod]
base_url = "https://erp.example.com"
company = "Example Corp"
seat_user = "seat@example.com"
api_key = "env:PACIOLI_PROD_KEY"
api_secret = "env:PACIOLI_PROD_SECRET"
default = true

[targets.staging]
base_url = "https://staging.example.com/"
api_key = "inline-key-is-ok"
api_secret = "file:/run/secrets/staging"
"""


class TestLoadRegistry(unittest.TestCase):
    def test_parses_targets_with_fields(self):
        reg = _load(VALID)
        t = reg.get("prod")
        self.assertEqual(t.base_url, "https://erp.example.com")
        self.assertEqual(t.company, "Example Corp")
        self.assertEqual(t.api_key, "env:PACIOLI_PROD_KEY")
        self.assertEqual(t.api_secret, "env:PACIOLI_PROD_SECRET")

    def test_base_url_trailing_slash_normalised(self):
        reg = _load(VALID)
        self.assertEqual(reg.get("staging").base_url, "https://staging.example.com")

    def test_company_optional(self):
        reg = _load(VALID)
        self.assertIsNone(reg.get("staging").company)

    def test_seat_user_parsed_and_optional(self):
        reg = _load(VALID)
        self.assertEqual(reg.get("prod").seat_user, "seat@example.com")
        self.assertIsNone(reg.get("staging").seat_user)  # absent → None (corroboration off)

    def test_blank_seat_user_is_none(self):
        reg = _load(VALID.replace('seat_user = "seat@example.com"', 'seat_user = "  "'))
        self.assertIsNone(reg.get("prod").seat_user)

    def test_posture_parsed_and_optional(self):
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nposture = "sole_door"'))
        self.assertEqual(reg.get("prod").posture, "sole_door")
        self.assertIsNone(reg.get("staging").posture)  # absent → None → mixed_door default

    def test_blank_posture_is_none(self):
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nposture = "   "'))
        self.assertIsNone(reg.get("prod").posture)

    def test_posture_typo_passes_through_for_runtime_deny_bias(self):
        # a string typo is NOT rejected at load — build_response deny-biases it to sole_door + flags
        # (single source of posture validation; a policy typo must not brick every operation).
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nposture = "sole_dooor"'))
        self.assertEqual(reg.get("prod").posture, "sole_dooor")

    def test_non_string_posture_refused_at_load(self):
        with self.assertRaises(RegistryError):
            _load(VALID.replace('seat_user = "seat@example.com"',
                                'seat_user = "seat@example.com"\nposture = 123'))

    def test_site_tz_parsed_and_optional(self):
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nsite_tz = "Asia/Kolkata"'))
        self.assertEqual(reg.get("prod").site_tz, "Asia/Kolkata")
        self.assertIsNone(reg.get("staging").site_tz)  # absent → None → no conversion (today)

    def test_blank_site_tz_is_none(self):
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nsite_tz = "   "'))
        self.assertIsNone(reg.get("prod").site_tz)

    def test_site_tz_typo_passes_through_for_runtime_refusal(self):
        # a string typo is NOT rejected at load — pacioli.clock refuses it AT USE with the zone
        # named (single source of zone validation; a typo must not brick non-windowed operations).
        reg = _load(VALID.replace('seat_user = "seat@example.com"',
                                  'seat_user = "seat@example.com"\nsite_tz = "Not/AZone"'))
        self.assertEqual(reg.get("prod").site_tz, "Not/AZone")

    def test_non_string_site_tz_refused_at_load(self):
        with self.assertRaises(RegistryError):
            _load(VALID.replace('seat_user = "seat@example.com"',
                                'seat_user = "seat@example.com"\nsite_tz = 123'))

    def test_inline_secret_refused(self):
        bad = VALID.replace('api_secret = "file:/run/secrets/staging"', 'api_secret = "s3cret-literal"')
        with self.assertRaises(RegistryError) as ctx:
            _load(bad)
        self.assertIn("by reference", str(ctx.exception))
        self.assertNotIn("s3cret-literal", str(ctx.exception))  # never echo the secret back

    def test_missing_required_field_refused(self):
        with self.assertRaises(RegistryError):
            _load('[targets.x]\nbase_url = "https://x.example.com"\napi_key = "k"\n')  # no api_secret

    def test_http_non_local_refused(self):
        with self.assertRaises(RegistryError) as ctx:
            _load('[targets.x]\nbase_url = "http://erp.example.com"\n'
                  'api_key = "k"\napi_secret = "env:S"\n')
        self.assertIn("https", str(ctx.exception).lower())

    def test_http_localhost_allowed(self):
        reg = _load('[targets.x]\nbase_url = "http://localhost:8000"\n'
                    'api_key = "k"\napi_secret = "env:S"\n')
        self.assertEqual(reg.get("x").base_url, "http://localhost:8000")

    def test_http_non_local_allowed_only_with_explicit_flag(self):
        reg = _load('[targets.x]\nbase_url = "http://192.0.2.10:8000"\n'
                    'api_key = "k"\napi_secret = "env:S"\nallow_http = true\n')
        self.assertEqual(reg.get("x").base_url, "http://192.0.2.10:8000")

    def test_garbage_url_refused(self):
        with self.assertRaises(RegistryError):
            _load('[targets.x]\nbase_url = "erp.example.com"\napi_key = "k"\napi_secret = "env:S"\n')

    def test_no_targets_refused(self):
        with self.assertRaises(RegistryError):
            _load("")

    def test_duplicate_default_refused(self):
        with self.assertRaises(RegistryError):
            _load(VALID.replace('api_key = "inline-key-is-ok"',
                                'default = true\napi_key = "inline-key-is-ok"'))


class TestTargetResolution(unittest.TestCase):
    def test_get_by_name(self):
        reg = _load(VALID)
        self.assertEqual(reg.get("staging").name, "staging")

    def test_unknown_name_refused_and_names_available(self):
        reg = _load(VALID)
        with self.assertRaises(RegistryError) as ctx:
            reg.get("nope")
        msg = str(ctx.exception)
        self.assertIn("prod", msg)
        self.assertIn("staging", msg)

    def test_default_explicit_flag(self):
        reg = _load(VALID)
        self.assertEqual(reg.get(None).name, "prod")

    def test_single_target_is_implicit_default(self):
        reg = _load('[targets.only]\nbase_url = "https://x.example.com"\n'
                    'api_key = "k"\napi_secret = "env:S"\n')
        self.assertEqual(reg.get(None).name, "only")

    def test_ambiguous_default_refused(self):
        no_default = VALID.replace("default = true\n", "")
        reg = _load(no_default)
        with self.assertRaises(RegistryError):
            reg.get(None)


class TestResolveAuth(unittest.TestCase):
    def _target(self, key="env:K", secret="env:S"):
        reg = _load(f'[targets.x]\nbase_url = "https://x.example.com"\n'
                    f'api_key = "{key}"\napi_secret = "{secret}"\n')
        return reg.get("x")

    def test_env_reference_resolved(self):
        k, s = resolve_auth(self._target(), env={"K": "kk", "S": "ss"}, read_file=None)
        self.assertEqual((k, s), ("kk", "ss"))

    def test_inline_key_passthrough(self):
        k, s = resolve_auth(self._target(key="literal-key"), env={"S": "ss"}, read_file=None)
        self.assertEqual(k, "literal-key")

    def test_missing_env_var_names_the_var_not_a_value(self):
        with self.assertRaises(RegistryError) as ctx:
            resolve_auth(self._target(), env={"K": "kk"}, read_file=None)
        self.assertIn("S", str(ctx.exception))

    def test_file_reference_resolved_and_stripped(self):
        def read_file(path):
            self.assertEqual(path, "/run/secrets/x")
            return "sssss\n"
        k, s = resolve_auth(self._target(secret="file:/run/secrets/x"),
                            env={"K": "kk"}, read_file=read_file)
        self.assertEqual(s, "sssss")

    def test_empty_resolved_secret_refused(self):
        with self.assertRaises(RegistryError):
            resolve_auth(self._target(), env={"K": "kk", "S": "  "}, read_file=None)


if __name__ == "__main__":
    unittest.main()
