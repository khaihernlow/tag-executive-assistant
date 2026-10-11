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


def test_blended_staff_names_are_corrected_or_flagged():
    from agent.factcheck import verify_names

    known = {"Cory Keller", "Chris Lawrence", "Dan Lawrence", "Riley Jones"}
    text, changes = verify_names("Cory Lawrence and Riley Jones logged little. Riley Smith too.", known)
    assert "Cory Lawrence (name unclear: Chris Lawrence or Cory Keller or Dan Lawrence)" in text
    assert "Riley Jones logged" in text
    assert "Riley Jones too" in text  # only one Riley: corrected
    assert changes == ["Cory Lawrence unclear", "Riley Smith -> Riley Jones"]
    # Ordinary capitalised words and names nobody listed are left alone.
    assert verify_names("Last Week, Sawyer Savings met Jordan Rivera.", known)[0] == "Last Week, Sawyer Savings met Jordan Rivera."


def test_dollar_amounts_must_come_from_the_data():
    import json

    from agent.factcheck import known_numbers, verify_dollars

    tool = json.dumps({"quotes": [{"one_time": 153422.5, "monthly": 1989.0}, {"one_time": 31939.0}],
                       "highlights": {"biggest": ["Masonic Care: PenTest ($16,800)"]}})
    known = known_numbers(tool, "what's out there?")
    reply = ("GP Fund is $153K plus $1,989/mo, Casella is $31,939, Masonic is $16.8K, "
             "and the total is $250K. Also $31.9K and $2,000/mo.")
    text, changes = verify_dollars(reply, known)
    assert "$153K plus" in text and "$1,989/mo" in text and "$31,939," in text and "$16.8K," in text
    assert "$250K (unverified)" in text            # nothing in the data says 250K
    assert "$31.9K." in text or "$31.9K and" in text   # 31,939 rounds to 31.9K
    assert "$2,000/mo (unverified)" in text        # 1,989 isn't 2,000 at full precision
    assert changes == ["$250K unverified", "$2,000/mo unverified"]
    assert verify_dollars("No money here.", known) == ("No money here.", [])
