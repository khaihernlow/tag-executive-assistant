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


def test_a_calculation_written_out_is_redone_in_code():
    from agent.factcheck import known_numbers, verify_dollars
    known = known_numbers("Managed services: 42 seats at $135 per seat per month. Onboarding fee: $2,500. Term: 36 months.")

    # The answer that prompted this: the working is right there, so the total is checked, not flagged
    text, changes = verify_dollars("Monthly total is $5,670 (42 seats x $135).", known)
    assert changes == [] and "(unverified)" not in text
    for reply in ("$135 \u00d7 42 = $5,670 a month", "42 seats at $135 comes to $5,670/mo",
                  "42 * $135 = $5,670", "$135 per seat times 42 is $5,670"):
        assert verify_dollars(reply, known)[1] == [], reply

    # Chains: the first result can be used in the next step
    assert verify_dollars("42 x $135 = $5,670, plus $5,670 + $2,500 = $8,170 the first month.", known)[1] == []

    # Wrong arithmetic is still caught
    assert verify_dollars("Monthly total is $5,760 (42 seats x $135).", known)[1] == ["$5,760 unverified"]
    # A product of numbers that aren't in the sources proves nothing
    assert verify_dollars("50 seats x $150 = $7,500.", known)[1] == ["$150 unverified", "$7,500 unverified"]
    # Working not shown stays flagged: 36 x $5,670 was done silently
    text, changes = verify_dollars("42 x $135 = $5,670 a month, about $204,000 over the term.", known)
    assert changes == ["$204,000 unverified"]
