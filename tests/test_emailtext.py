from agent.emailtext import excerpt, readable

SIG = "Dave Vener President TAG Solutions, LLC 518‑292‑6550 www.tagsolutions.com ​​​"


def test_daves_signature_goes():
    assert readable(f"Excellent, we will be there. Have a nice weekend. {SIG}") == \
        "Excellent, we will be there. Have a nice weekend."
    assert readable(f"Hi Luke, can you send me those links? Let me know, Dave {SIG}") == \
        "Hi Luke, can you send me those links? Let me know, Dave"
    assert readable("Hey Elizabeth, Circling back. Thanks, Dave Dave​ Vener President TAG Solutions, LLC "
                    "518.292.6595 www.tagsolutions.com Schedule time with me") == "Hey Elizabeth, Circling back. Thanks, Dave"
    # A colleague's signature too
    assert readable("Paul or Dave you good with this? Wolf Frohnhoefer Technical Alignment Manager "
                    "TAG Solutions, LLC 518‑292‑6550 www.tagsolutions.com") == "Paul or Dave you good with this?"


def test_banners_logos_and_phone_apps_go():
    text = ("[This email is from a trusted sender and has a valid signature.] Thank you for the update Dave. "
            "Have a great rest of the day! Regards, Tom Thomas McLaughlin [A blue and grey logo Description "
            "automatically generated] Executive Vice President 555-123-4567")
    assert readable(text) == "Thank you for the update Dave. Have a great rest of the day! Regards, Tom"
    assert readable("[This email has a low security risk because it is a bulk message.] I have added Bob. "
                    "Regards, Linda Get Outlook for iOS") == "I have added Bob. Regards, Linda"
    assert readable("​Hi Dave, any update? Warm Regards, [iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk]") \
        == "Hi Dave, any update? Warm Regards,"


def test_a_thanks_that_opens_the_email_stays():
    assert readable(f"Thanks, Bob. I look forward to meeting you. I sent an invite just now. {SIG}") == \
        "Thanks, Bob. I look forward to meeting you. I sent an invite just now."


def test_quoted_history_and_teams_block_go():
    assert readable(f"Kyle or Tim, will you be joining? {SIG} From: dvener@tagsolutions.com When: 10:30 AM "
                    "Subject: Planning Location: Teams") == "Kyle or Tim, will you be joining?"
    assert readable("This meeting is to plan the November event. ______________________ Microsoft Teams meeting "
                    "Join: Meeting ID: 248 206 49 Passcode: x") == "This meeting is to plan the November event."
    assert readable("Sounds good. On Mon, Oct 5, 2026 at 9:00 AM Bob Smith <bob@x.com> wrote: earlier text") == "Sounds good."


def test_a_forward_with_nothing_added_shows_what_was_forwarded():
    body = (f"{SIG} From: Fanor Paz <fanor@recruit.example> Sent: Monday, October 12, 2026 7:00 AM To: Dave Vener "
            "Subject: Technical Account Manager – Recruiting Support Hi Dave, I have three candidates for the "
            "TAM role and can send profiles today. Best, Fanor Fanor Paz Recruiter 555-123-4567")
    assert readable(body, "FW: Technical Account Manager – Recruiting Support") == \
        "Hi Dave, I have three candidates for the TAM role and can send profiles today. Best, Fanor"
    # Not a forward: an empty reply stays empty rather than showing someone else's message
    assert readable(body, "Re: Technical Account Manager") == ""


def test_calendar_responses_are_empty_and_long_text_is_cut_at_a_word():
    assert readable("") == ""
    long = "word " * 200
    out = excerpt(long, limit=50)
    assert len(out) <= 51 and out.endswith("…") and not out[:-1].endswith(" ")


def test_a_thanks_in_the_middle_is_not_a_sign_off():
    text = "Hi all, see attached. Thanks, Maria for organizing this, we will meet Tuesday. Best, Joe"
    assert readable(text) == text
    assert readable("Hi all, see attached. Thanks, Maria for organizing this. Best, Joe Smith CEO 555-123-4567") == \
        "Hi all, see attached. Thanks, Maria for organizing this. Best, Joe"


def test_capitalised_words_before_the_signature_stay():
    assert readable(f"Kelly, See notes from Peconic Landing {SIG}") == "Kelly, See notes from Peconic Landing"
    assert readable(f"I've updated the time so Kyle can meet with Ximena. -Maria {SIG}") == \
        "I've updated the time so Kyle can meet with Ximena. -Maria"


def test_meeting_recap_wrapping_and_bare_signatures_go():
    assert readable("[ Meeting notes \u2726 Ask AI about this meeting Bob/Dave - IT discussion View full transcript "
                    "Notes were taken by AI. Purpose Discuss IT staffing.") == "Bob/Dave - IT discussion Purpose Discuss IT staffing."
    assert readable("[HubSpot] Chelsi visited the pricing page.") == "Chelsi visited the pricing page."
    assert readable("It is all here. Thanks. MJM Direct 614.973.5061 Book time with Mike") == "It is all here. Thanks."
    # A phone number in the message itself stays
    assert readable("Please call our main office at 479-721-2200 for assistance. Thank you.") == \
        "Please call our main office at 479-721-2200 for assistance. Thank you."
