# Rotary Sign server: guide for IT

Rotary Sign is a self-hosted service for sending PDFs out for electronic signature. Staff log in, upload a PDF, add signers, place signature, date, name, text and checkbox fields, and send. Each signer gets an emailed link. They agree to sign electronically, then sign in their browser. No account or software is needed on their side.

When everyone has signed, the server does four things:

1. Stamps the fields onto the PDF.
2. Appends a certificate of completion: who signed, when, from which IP address, and the full event history.
3. Seals the file with a digital signature, so any later change is detectable.
4. Emails the finished copy to everyone.

It's one Python process with an SQLite database and a folder of files. There's no external database, queue or cloud service.

## Quick trial (5 minutes)

```bash
pip install ".[server]"                      # from a checkout of this repository
rotary-sign run                              # serves http://127.0.0.1:8080
```

The console prints a **setup code**. Open http://127.0.0.1:8080, enter the code, and create the first administrator. Email isn't configured yet, so messages land in `rotary-sign-data/outbox/` and the sender can copy each signing link from the screen.

## Production install

### Option A: Docker (recommended)

```bash
docker compose up -d                         # edit docker-compose.yml first: URL, organisation, email
docker compose logs rotary-sign              # shows the setup code on first start
```

All state lives in the `rotary-sign-data` volume, mounted at `/data`.

### Option B: Windows Server or Linux, without Docker

You need Python 3.11+.

```bash
pip install ".[server]"
rotary-sign --data D:\RotarySign run --host 127.0.0.1 --port 8080
```

To run it as a service:

* **Windows**: use [NSSM](https://nssm.cc/), for example
  `nssm install RotarySign "C:\Program Files\Python312\Scripts\rotary-sign.exe" "--data D:\RotarySign run --port 8080"`,
  then `nssm start RotarySign`. Put settings in `D:\RotarySign\rotary-sign.toml`.
* **Linux**: use a systemd unit:

  ```ini
  [Service]
  ExecStart=/opt/rotary/venv/bin/rotary-sign --data /var/lib/rotary-sign run --port 8080
  User=rotary
  Restart=always
  [Install]
  WantedBy=multi-user.target
  ```

### HTTPS and the reverse proxy

Run Rotary Sign behind your usual HTTPS reverse proxy (IIS with URL Rewrite/ARR, nginx, Caddy, or a load balancer). Then set:

```toml
url = "https://sign.example.com"   # the address in emailed links
trust_proxy = true                  # record signers' real IP addresses from X-Forwarded-For
```

With Caddy, the whole proxy config is `sign.example.com { reverse_proxy 127.0.0.1:8080 }`.

**Reachability.** If you send documents to customers or suppliers, `/s/…` (signing links) and `/static/…` must be reachable from the internet. Everything else can stay internal-only. Senders' pages need a login either way.

## Settings

Copy [`rotary-sign.example.toml`](../rotary-sign.example.toml) to `<data folder>/rotary-sign.toml`. Every setting can instead be set as an environment variable `ROTARY_SIGN_<NAME>`, which is how the Docker setup does it.

`rotary-sign check` prints the settings in use, tests the email login and loads the seal certificate.

### Email

| Provider | Host | Port | Security | Login |
| --- | --- | --- | --- | --- |
| Microsoft 365 | smtp.office365.com | 587 | starttls | a licensed mailbox (SMTP AUTH must be enabled for it) |
| Google Workspace | smtp.gmail.com | 587 | starttls | an app password |
| Internal relay | your relay | 25 | none | usually none |

Failed sends are recorded in the envelope history. The sender can resend, or copy a fresh link, from the envelope page.

### The seal certificate

Every completed document is sealed with a PAdES digital signature that covers the whole file.

* **Default**: a self-signed certificate is created in `<data>/seal/` on first use. Tampering is still detected (the *Check a document* page and `rotary-sign verify` both report it). Adobe Reader, however, shows the signature as "validity unknown", because it doesn't know your certificate.
* **Trusted inside the company**: distribute `<data>/seal/seal-cert.pem` (also downloadable from `/seal-certificate.pem`) as a trusted certificate through Group Policy or Acrobat's trust settings.
* **Trusted everywhere**: buy a document-signing certificate from a CA on Adobe's Approved Trust List and set `seal_p12` and `seal_password`. Optionally set `timestamp_url` to a public RFC 3161 timestamp service, so signing times are independently attested.

## Accounts

* The first administrator is created at `/setup`, which needs the code from the console. Setup closes as soon as an administrator exists.
* Administrators add senders under **Users**. A new account gets a temporary password, which the person can change under **Account**. You can also create accounts with `rotary-sign add-user email --name "Name" [--admin]`.
* People who only sign never need an account.
* After 10 failed logins in 15 minutes, further attempts are blocked for that address.

## Backups and retention

Back up the whole data folder:

* `rotary-sign.db`: users, envelopes, audit trail
* `documents/`: originals, signed copies, page images
* `seal/`: the sealing key
* `rotary-sign.toml`: settings

Treat `seal/seal-key.pem` like a password. Copy the folder while the service is stopped, or use SQLite's online backup (`sqlite3 rotary-sign.db ".backup copy.db"`).

Only drafts can be deleted. Sent and completed envelopes are kept as records.

## Security model

* **Signing links.** Each link carries a 256-bit random secret. Only its SHA-256 hash is stored, so a leaked database doesn't expose live links. Links expire after `link_days` (30 by default) and are revoked when an envelope is voided or a new link is issued.
* **Signers can only act on their own fields.** In sequential mode they can't act before their turn.
* **Sender sessions** use HTTP-only, SameSite cookies that are Secure when the URL is HTTPS. Every state-changing request needs a CSRF token. Passwords are hashed with scrypt.
* **Browser protections.** A strict Content-Security-Policy (no inline scripts), `X-Frame-Options: DENY` (so signing pages can't be framed or clickjacked) and `Referrer-Policy: no-referrer`.
* **Tamper-evident audit trail.** Each event stores a hash chained to the previous one; the envelope page warns if the chain is broken. The finished PDF carries a copy of the trail on its certificate page and is sealed.

## Legal notes (not legal advice)

Electronic signatures are legally recognised for most business documents:

* in the US, under the ESIGN Act and UETA
* in the EU, as simple electronic signatures under eIDAS
* in the UK, under the Electronic Communications Act 2000 and common law

Rotary Sign records what those laws look for:

* **Consent**: each signer agrees to sign electronically before signing, after a disclosure covering paper copies and withdrawal.
* **Intent**: each signer adopts a signature explicitly.
* **Attribution**: the signer's email, IP address, browser and timestamps are recorded.
* **Integrity and retention**: the sealed PDF, its certificate and the chained audit trail.

Check with your legal team before using it for:

* **Documents most e-signature laws exclude**: wills and trusts, family-law matters, court documents, and certain consumer notices (eviction, foreclosure, utility shut-off, insurance cancellation).
* **Regulated workflows** with extra rules, such as FDA 21 CFR Part 11, HIPAA, and some financial or government forms. Part 11, for example, requires signers to re-authenticate with their own credentials and to record the meaning of each signature. Rotary Sign doesn't do that today.
* **EU "advanced" or "qualified" signatures** (AES/QES). These need verified signer identities and, for QES, a qualified trust service provider.

## What it doesn't do (yet)

Compared with commercial services, Rotary Sign doesn't yet have:

* stronger signer identity checks (SMS codes, ID verification)
* reusable templates
* automatic reminders
* bulk sending
* signer attachments
* integrations (Salesforce, Microsoft 365)
* compliance certifications such as SOC 2

Those are reasonable next steps if your use needs them.
