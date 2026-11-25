# SPDX-License-Identifier: MIT
"""Audit log redaction: URL query values that carry credentials never reach disk.
Run: .venv/bin/python tests/test_audit.py"""
import _helpers

_helpers.isolated_home()
import cased  # noqa: E402


def test_scrub_redacts_credential_params_and_keeps_the_rest():
    body = '{"url":"https://x.y/cb?state=s1&code=AUTHCODE&next=/home#access_token=TOK&type=bearer"}'
    assert cased._scrub(body) == (
        '{"url":"https://x.y/cb?state=s1&code=[redacted]&next=/home#access_token=[redacted]&type=bearer"}')


def test_scrub_is_case_insensitive_and_handles_api_key_variants():
    assert cased._scrub("?API_KEY=k1&apikey=k2&Sig=abc") == "?API_KEY=[redacted]&apikey=[redacted]&Sig=[redacted]"


def test_scrub_handles_percent_encoded_delimiters_in_nested_urls():
    body = "redirect_uri=https%3A%2F%2Fapp%2Fcb%3Fcode%3DSECRET%26state%3Ds1&scope=openid"
    assert cased._scrub(body) == "redirect_uri=https%3A%2F%2Fapp%2Fcb%3Fcode%3D[redacted]%26state%3Ds1&scope=openid"


def test_scrub_redacts_a_query_that_starts_with_a_secret():
    assert cased._scrub("?" + "code=SECRET&state=s1")[1:] == "code=[redacted]&state=s1"


def test_scrub_leaves_ordinary_bodies_alone():
    body = '{"expression":"document.title","url":"https://a.b/?q=code%20review&page=2"}'
    assert cased._scrub(body) == body


if __name__ == "__main__":
    _helpers.run_tests(globals())
