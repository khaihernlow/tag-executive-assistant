from agent.factcheck import known_addresses, verify_addresses


def test_corrects_a_blended_address_to_the_real_one_at_that_domain():
    known = known_addresses('{"attendees": [{"email": "sam@staffing.example"}]}', "dave@tag.example")
    text, changes = verify_addresses("Also on the invite is Sam (samuel@staffing.example).", known)
    assert text == "Also on the invite is Sam (sam@staffing.example)."
    assert changes == ["samuel@staffing.example -> sam@staffing.example"]


def test_known_addresses_pass_and_unknown_domains_are_flagged():
    known = known_addresses("klow@tag.example gstone@tag.example")
    text, changes = verify_addresses("Email klow@tag.example or someone@acme.com", known)
    assert text == "Email klow@tag.example or someone@acme.com (unverified)"
    assert changes == ["someone@acme.com unverified"]


def test_ambiguous_domain_picks_the_closest():
    known = known_addresses("gstone@tag.example jyoung@tag.example")
    text, _ = verify_addresses("gsmyth@tag.example", known)
    assert text == "gstone@tag.example"


def test_chat_replies_lose_long_dashes_but_keep_bullets_and_hyphens():
    from agent.factcheck import no_long_dashes

    text = "- **Oct 6, 3:00–3:30 PM** — You interviewed Jordan\n- Follow-up call"
    assert no_long_dashes(text) == "- **Oct 6, 3:00-3:30 PM**, You interviewed Jordan\n- Follow-up call"
    assert no_long_dashes("she isn't in Autotask -- add her first") == "she isn't in Autotask, add her first"
    assert no_long_dashes("--verbose flag") == "--verbose flag"  # not a dash between words
