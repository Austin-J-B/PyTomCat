# Privacy Policy — TomCat

**Effective date:** August 7, 2026
**Applies to:** the TomCat Discord bot and the membership tools operated by Campus Cat Coalition
**Contact:** utacampuscats@gmail.com

Campus Cat Coalition is a student organization at the University of Texas at
Arlington that cares for the campus cat population. "TomCat" is the Discord bot
we run to handle club membership, dues reconciliation, feeding logs, and cat
photo records. This policy explains what the bot collects, why, and what you can
ask us to do about it.

---

## Google account data

TomCat connects to **one Google account: the club's own mailbox**
(`utacampuscats@gmail.com`). It does **not** connect to, request access to, or
read the Google account of any member, visitor, or other user.

**Scope requested:** `https://www.googleapis.com/auth/gmail.readonly` —
read-only access. The bot cannot send, modify, archive, or delete mail.

**Why we need it.** Members pay dues through PayPal, Venmo, and Cash App. Those
services email a payment notification to the club mailbox. The bot reads those
notifications so it can match a payment to the member who made it, mark their
dues as verified, and record the amount in the club's financial records. Without
it, an officer would have to reconcile every payment by hand.

**What the bot reads and stores.** From messages in the club inbox it records the
subject line, sender, message and thread identifiers, timestamps, and message
body. From payment notifications it extracts the payer's display name, the
amount, and any note the payer attached to the payment.

The club mailbox also receives ordinary correspondence. The bot's scan is not
limited to payment notifications, so incoming mail may be stored in our logs
even when it is unrelated to dues. We do not use that mail for any purpose other
than the payment reconciliation described above.

**We also use Google Sheets** (through a separate service account, not your data)
to store the club's membership roster and financial records.

### Limited Use disclosure

TomCat's use and transfer of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

Specifically, we do not use Google user data for advertising, we do not sell it,
we do not transfer it to third parties except as needed to provide the club's
membership features or as required by law, and we do not allow humans to read it
except where a club officer needs to resolve a specific payment question, where
you have asked us to, or where required by law.

---

## Discord and membership data

To run the club, TomCat also handles:

- **Discord account information** — your user ID, username, display name, and
  the roles you hold in the club server.
- **Messages in club channels** — the bot logs message content, author, channel,
  and attachments from the channels it monitors. This supports dues processing,
  moderation, and spam detection.
- **Membership application responses** — full name, email address, phone number,
  Discord username, the name your payment will arrive under, membership type,
  and the committees you are interested in.
- **Payment details** — the name on the payment, the amount, the service used,
  the date, and any note you attached. **We never see or store your card, bank,
  or payment-service login details.** Those stay with PayPal, Venmo, and Cash App.
- **Photos** — cat photos submitted to the server, and any proof-of-payment image
  you choose to upload.

---

## Where it goes and who can see it

Data is stored in the club's Google Sheets and in log files on a private server
that only club officers can access.

We do **not** sell your information or share it for advertising. It is visible to
club officers who need it for club business, and it is held by the service
providers we depend on to operate: Google (Gmail, Sheets), Discord, our server
host, and Modal (which runs the cat-photo recognition models). We may disclose
information if the law requires it.

## How long we keep it

Membership records are kept for as long as you are a member and for up to two
years afterward, so we can answer questions about past dues and produce the
financial history a nonprofit is expected to keep.

Operational logs, including stored message and email content, are retained for
**24 months** and then deleted.

## Your choices

Write to **utacampuscats@gmail.com** and you may ask us to:

- tell you what we hold about you,
- correct anything that is wrong,
- delete your membership record and associated logs.

We will honor deletion requests except where we are required to keep a record of
a financial transaction. Please allow up to 30 days.

You can also remove yourself at any time by leaving the Discord server, though
that alone does not erase records we already hold — email us if you want those
removed too.

## Children

The club serves university students. TomCat is not directed at children under 13,
and we do not knowingly collect their information. If you believe a child under
13 has given us information, contact us and we will delete it.

## Changes

If we change this policy we will update the effective date above and announce it
in the club Discord server.

---

*Questions about this policy, or about anything TomCat stores: utacampuscats@gmail.com*
