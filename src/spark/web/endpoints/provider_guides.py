"""Provider setup guide endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/help/provider")

# ---------------------------------------------------------------------------
# Helpers to reduce duplication in guide definitions
# ---------------------------------------------------------------------------

_ICON_EXTERNAL = "bi-box-arrow-up-right"
_ICON_BOOK = "bi-book"
_ICON_PRICE = "bi-currency-dollar"


def _link(label: str, url: str, icon: str = _ICON_EXTERNAL) -> dict:
    return {"label": label, "url": url, "icon": icon}


def _step(
    title: str,
    description: str,
    *,
    screenshot_placeholder: str | None = None,
    substeps: list[str] | None = None,
    tip: str | None = None,
) -> dict:
    d: dict = {"title": title, "description": description}
    if screenshot_placeholder:
        d["screenshot_placeholder"] = screenshot_placeholder
    if substeps:
        d["substeps"] = substeps
    if tip:
        d["tip"] = tip
    return d


def _trouble(problem: str, solution: str) -> dict:
    return {"problem": problem, "solution": solution}


# ---------------------------------------------------------------------------
# Guide definitions: getting an account and a key from each model service
# ---------------------------------------------------------------------------

PROVIDER_GUIDES: dict[str, dict] = {
    "anthropic": {
        "id": "anthropic",
        "title": "Anthropic",
        "icon": "bi-chat-square-dots",
        "summary": (
            "Connect to Claude models via the Anthropic API.  This guide walks you "
            "through creating an Anthropic account and generating an API key."
        ),
        "links": [
            _link("Anthropic Console", "https://console.anthropic.com/"),
            _link(
                "API Documentation", "https://docs.anthropic.com/en/api/getting-started", _ICON_BOOK
            ),
            _link("Pricing", "https://www.anthropic.com/pricing", _ICON_PRICE),
        ],
        "prerequisites": [
            "An email address to create an Anthropic account",
            "A payment method (credit card) — the API is usage-based",
        ],
        "steps": [
            {
                "title": "Create an Anthropic account",
                "description": (
                    "Visit the Anthropic Console and click <strong>Sign Up</strong>. "
                    "You can register with an email address or use Google / GitHub SSO."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Anthropic Console sign-up page "
                    "(console.anthropic.com — Sign Up button)"
                ),
            },
            {
                "title": "Complete account verification",
                "description": (
                    "Anthropic will send a verification email.  Click the link in "
                    "the email to verify your account, then log in to the Console."
                ),
                "screenshot_placeholder": ("Screenshot: Verification email from Anthropic"),
            },
            {
                "title": "Add billing information",
                "description": (
                    "Navigate to <strong>Settings → Billing</strong> in the Console "
                    "and add a payment method.  API access requires an active billing "
                    "profile, even for the free tier credits Anthropic may provide."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Anthropic Console — Settings → Billing page "
                    "showing payment method form"
                ),
                "tip": (
                    "Anthropic offers free trial credits for new accounts.  Check "
                    "the billing page for your current credit balance."
                ),
            },
            {
                "title": "Navigate to API Keys",
                "description": (
                    "In the Console, go to <strong>Settings → API Keys</strong>.  "
                    "This page lists all your existing keys and lets you create new ones."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Anthropic Console — Settings → API Keys page"
                ),
            },
            {
                "title": "Create a new API key",
                "description": (
                    "Click <strong>Create Key</strong>.  Give the key a descriptive "
                    'name (e.g. "Spark") so you can identify it later.  The key '
                    "will be displayed once — copy it immediately."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Anthropic Console — Create Key dialog "
                    "showing name field and generated key"
                ),
                "tip": (
                    "Store the key somewhere safe before closing the dialog.  "
                    "Anthropic will not show the full key again — you would need "
                    "to create a new one if lost."
                ),
            },
        ],
        "spark_config": [
            {
                "title": "Add an Anthropic connection",
                "description": (
                    "In Spark, open <strong>Connections</strong> (personal mode: the first "
                    "section of <strong>Settings</strong>; a server: <strong>Account → "
                    "Connections</strong>), choose <strong>Add connection</strong> and pick "
                    "<strong>Anthropic</strong>.  Give the connection a name."
                ),
            },
            {
                "title": "Enter the API key",
                "description": (
                    "Paste the API key you copied from the Anthropic Console into <strong>API key</strong>.  The key goes straight to the secrets store and is "
                    "never shown again."
                ),
            },
            {
                "title": "Save and check its models",
                "description": (
                    "Click <strong>Save</strong>.  Spark checks the settings and then which "
                    "models the connection can reach.  Open the connection's "
                    "<strong>Models</strong> to enable the models you want to offer, then start a conversation with a Claude model."
                ),
            },
        ],
        "troubleshooting": [
            _trouble(
                "Authentication error (401)",
                (
                    "Double-check that the API key is correct and has not been "
                    "revoked.  Ensure there are no leading/trailing spaces."
                ),
            ),
            _trouble(
                "Insufficient credits / billing error",
                (
                    "Visit the Anthropic Console billing page and ensure a valid "
                    "payment method is on file and you have available credits."
                ),
            ),
            _trouble(
                "Rate limit exceeded (429)",
                (
                    "You have exceeded your usage tier limits.  Wait a moment and "
                    "retry, or upgrade your usage tier in the Anthropic Console."
                ),
            ),
        ],
    },
    "aws_bedrock": {
        "id": "aws_bedrock",
        "title": "AWS Bedrock",
        "icon": "bi-cloud",
        "summary": (
            "Access Claude and other foundation models through Amazon Bedrock.  "
            "This guide covers AWS account setup, model access requests, and "
            "credential configuration."
        ),
        "links": [
            _link("AWS Console", "https://console.aws.amazon.com/bedrock/"),
            _link(
                "Bedrock Documentation",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html",
                _ICON_BOOK,
            ),
            _link("Bedrock Pricing", "https://aws.amazon.com/bedrock/pricing/", _ICON_PRICE),
        ],
        "prerequisites": [
            "An AWS account (or permission to create one within your organisation)",
            "IAM permissions to enable Bedrock and request model access",
            "AWS CLI installed and configured (for SSO or IAM credential methods)",
        ],
        "steps": [
            {
                "title": "Create or sign in to your AWS account",
                "description": (
                    "Go to the AWS Management Console and sign in.  If you do not "
                    "have an account, click <strong>Create an AWS Account</strong> "
                    "and follow the registration process."
                ),
                "screenshot_placeholder": ("Screenshot: AWS Management Console sign-in page"),
            },
            {
                "title": "Navigate to Amazon Bedrock",
                "description": (
                    "In the AWS Console, search for <strong>Bedrock</strong> in the "
                    "services search bar, or navigate to "
                    "<strong>Services → Machine Learning → Amazon Bedrock</strong>."
                ),
                "screenshot_placeholder": (
                    "Screenshot: AWS Console — searching for Bedrock in the " "services search bar"
                ),
                "tip": (
                    "Bedrock is not available in all AWS regions.  Ensure you are "
                    "in a supported region (e.g. us-east-1, us-west-2, eu-west-1)."
                ),
            },
            {
                "title": "Request model access",
                "description": (
                    "In the Bedrock console, go to <strong>Model access</strong> "
                    "in the left sidebar.  Click <strong>Manage model access</strong> "
                    "and enable the models you want to use (e.g. Anthropic Claude "
                    "models).  Some models require you to submit a use-case form "
                    "and wait for approval."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Bedrock Console — Model access page showing "
                    "available models with enable checkboxes"
                ),
                "substeps": [
                    "Click <strong>Manage model access</strong>",
                    "Tick the checkbox next to each desired model (e.g. Claude Sonnet, Claude Opus)",
                    "Click <strong>Request model access</strong>",
                    "Wait for approval — most Anthropic models are approved instantly",
                ],
            },
            {
                "title": "Configure AWS credentials",
                "description": (
                    "Spark needs AWS credentials to call Bedrock.  Choose one of "
                    "the supported authentication methods:"
                ),
                "substeps": [
                    "<strong>The AWS credential chain</strong>: whatever the AWS CLI on this machine uses, for example <code>aws configure sso</code> and <code>aws sso login</code>, or an instance or container role.  This is the recommended method for organisations using AWS IAM Identity Centre.",
                    "<strong>AWS access keys</strong>: an IAM user's access key and secret key, with a session token for temporary credentials from <code>aws sts get-session-token</code>.",
                    "<strong>Short-term Bedrock API keys</strong>: generated by Spark from either of the above and refreshed before they expire.",
                ],
                "tip": (
                    "The IAM user or role needs at minimum the "
                    "<code>bedrock:InvokeModel</code> and "
                    "<code>bedrock:InvokeModelWithResponseStream</code> permissions, and "
                    "<code>bedrock:ListFoundationModels</code> and "
                    "<code>bedrock:ListInferenceProfiles</code> for model discovery."
                ),
            },
            {
                "title": "Verify AWS CLI access",
                "description": (
                    "Run <code>aws bedrock list-foundation-models --region us-east-1</code> "
                    "in your terminal.  If you see a list of models, your credentials "
                    "are configured correctly."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Terminal showing successful output of "
                    "aws bedrock list-foundation-models command"
                ),
            },
        ],
        "spark_config": [
            {
                "title": "Add an Amazon Bedrock connection",
                "description": (
                    "In Spark, open <strong>Connections</strong> (personal mode: the first "
                    "section of <strong>Settings</strong>; a server: <strong>Account → "
                    "Connections</strong>), choose <strong>Add connection</strong> and pick "
                    "<strong>Amazon Bedrock</strong>.  Give the connection a name."
                ),
            },
            {
                "title": "Set the Region and authentication",
                "description": (
                    "Enter the Region where you enabled model access (for example "
                    "<code>ap-southeast-2</code>) and choose the authentication: the AWS "
                    "credential chain (optionally a named profile), AWS access keys, or "
                    "short-term Bedrock API keys generated from either.  A long-term "
                    "Bedrock API key is offered only once <strong>Allow long-term Bedrock "
                    "API keys</strong> is ticked."
                ),
            },
            {
                "title": "Save and check its models",
                "description": (
                    "Click <strong>Save</strong>.  Spark checks the settings and then which "
                    "models the connection can reach.  Open the connection's "
                    "<strong>Models</strong> to enable the models you want to offer.  Geographic and global inference profiles are refused until the connection's residency allows them."
                ),
            },
        ],
        "troubleshooting": [
            _trouble(
                "No models appear in the model picker",
                (
                    "Ensure you have requested and been granted access to models "
                    "in the Bedrock console.  Also verify the connection's Region "
                    "matches the Region where you enabled model access, and that the "
                    "models are enabled in the connection's <strong>Models</strong>."
                ),
            ),
            _trouble(
                "Access denied / credential errors",
                (
                    "Run <code>aws sts get-caller-identity</code> to verify your "
                    "credentials are valid.  For SSO, you may need to run "
                    "<code>aws sso login</code> to refresh your session."
                ),
            ),
            _trouble(
                "Timeout connecting to Bedrock",
                (
                    "Check your network connection and ensure outbound HTTPS "
                    "traffic to AWS endpoints is not blocked by a firewall or proxy."
                ),
            ),
        ],
    },
    "ollama": {
        "id": "ollama",
        "title": "Ollama",
        "icon": "bi-pc-display",
        "summary": (
            "Run AI models locally on your own machine using Ollama.  No API key "
            "or cloud account required — everything stays on your computer."
        ),
        "links": [
            _link("Ollama Website", "https://ollama.com/"),
            _link("Model Library", "https://ollama.com/library", "bi-collection"),
            _link("GitHub", "https://github.com/ollama/ollama", "bi-github"),
        ],
        "prerequisites": [
            "macOS 12+, Windows 10+, or Linux",
            "Sufficient RAM for the models you want to run (8 GB minimum, 16 GB+ recommended)",
            "Sufficient disk space for model files (models range from 2 GB to 40 GB+)",
        ],
        "steps": [
            {
                "title": "Download Ollama",
                "description": (
                    "Visit <strong>ollama.com</strong> and download the installer "
                    "for your operating system.  On macOS, you can also install "
                    "via Homebrew: <code>brew install ollama</code>."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Ollama download page (ollama.com) showing " "platform options"
                ),
            },
            {
                "title": "Install Ollama",
                "description": (
                    "Run the installer and follow the prompts.  On macOS, drag "
                    "Ollama to your Applications folder.  On first launch, Ollama "
                    "installs its CLI tools and starts the background service."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Ollama installer / Applications folder on macOS"
                ),
            },
            {
                "title": "Pull a model",
                "description": (
                    "Open a terminal and run a pull command to download a model.  " "For example:"
                ),
                "substeps": [
                    "<code>ollama pull llama3.3</code> — Meta's Llama 3.3 (good general purpose, ~4 GB)",
                    "<code>ollama pull qwen3</code> — Alibaba's Qwen 3 (strong reasoning, ~5 GB)",
                    "<code>ollama pull mistral</code> — Mistral 7B (fast, lightweight, ~4 GB)",
                    "<code>ollama pull gemma3</code> — Google's Gemma 3 (efficient, ~3 GB)",
                ],
                "screenshot_placeholder": ("Screenshot: Terminal showing ollama pull progress"),
                "tip": (
                    "Browse the full model library at ollama.com/library to find "
                    "models suited to your use case and hardware."
                ),
            },
            {
                "title": "Verify Ollama is running",
                "description": (
                    "Run <code>ollama list</code> to see your downloaded models.  "
                    "The Ollama service runs automatically in the background.  You "
                    "can also test a model with <code>ollama run llama3.3</code> "
                    "to start an interactive chat."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Terminal showing ollama list output with " "downloaded models"
                ),
            },
        ],
        "spark_config": [
            {
                "title": "Add an Ollama connection",
                "description": (
                    "In Spark, open <strong>Connections</strong> (personal mode: the first "
                    "section of <strong>Settings</strong>; a server: <strong>Account → "
                    "Connections</strong>), choose <strong>Add connection</strong> and pick "
                    "<strong>Ollama</strong>.  Give the connection a name."
                ),
            },
            {
                "title": "Set the base URL",
                "description": (
                    "The default URL is <code>http://127.0.0.1:11434</code>, which "
                    "is correct for a standard local installation.  Only change "
                    "this if Ollama is running on a different machine or port."
                ),
            },
            {
                "title": "Save and check its models",
                "description": (
                    "Click <strong>Save</strong>.  Spark checks the settings and then which "
                    "models the connection can reach.  Open the connection's "
                    "<strong>Models</strong> to enable the models you want to offer, and declare each pulled model Spark's catalogue does not know yet."
                ),
            },
        ],
        "troubleshooting": [
            _trouble(
                "Connection refused / cannot reach Ollama",
                (
                    "Ensure the Ollama application is running.  On macOS, check "
                    "for the Ollama icon in the menu bar.  You can also start it "
                    "manually with <code>ollama serve</code>."
                ),
            ),
            _trouble(
                "No models appear in the model picker",
                "You need to pull at least one model first.  Run "
                "<code>ollama pull llama3.1:8b</code> (or another model) in your terminal, "
                "then check the connection's <strong>Models</strong> and declare any "
                "model listed as not in the catalogue.",
            ),
            _trouble(
                "Model runs very slowly",
                (
                    "Larger models require more RAM and compute.  Try a smaller "
                    "model variant, or check that no other heavy applications are "
                    "consuming system resources."
                ),
            ),
        ],
    },
    "google_gemini": {
        "id": "google_gemini",
        "title": "Google Gemini",
        "icon": "bi-stars",
        "summary": (
            "Connect to Google's Gemini models via the Google AI Studio API.  "
            "This guide walks you through obtaining a free API key."
        ),
        "links": [
            _link("Google AI Studio", "https://aistudio.google.com/"),
            _link("API Documentation", "https://ai.google.dev/gemini-api/docs", _ICON_BOOK),
            _link("Pricing", "https://ai.google.dev/gemini-api/docs/pricing", _ICON_PRICE),
        ],
        "prerequisites": [
            "A Google account (personal Gmail or Google Workspace)",
        ],
        "steps": [
            {
                "title": "Sign in to Google AI Studio",
                "description": (
                    "Visit <strong>aistudio.google.com</strong> and sign in with "
                    "your Google account.  If this is your first visit, you may "
                    "need to accept the terms of service."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Google AI Studio landing page with sign-in"
                ),
            },
            {
                "title": "Navigate to API Keys",
                "description": (
                    "Click <strong>Get API key</strong> in the left sidebar (or "
                    "from the top navigation).  This takes you to the API key "
                    "management page."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Google AI Studio — left sidebar showing " "'Get API key' option"
                ),
            },
            {
                "title": "Create an API key",
                "description": (
                    "Click <strong>Create API key</strong>.  You may be asked to "
                    "select or create a Google Cloud project — the default project "
                    "is fine for personal use.  The API key will be generated and "
                    "displayed."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Google AI Studio — API key creation dialog "
                    "showing the generated key"
                ),
                "tip": (
                    "The Gemini API has a generous free tier.  Check the pricing "
                    "page for current rate limits and quotas."
                ),
            },
            {
                "title": "Copy the API key",
                "description": (
                    "Click the copy button next to the generated key.  Store it "
                    "securely — you can always return to this page to view or "
                    "regenerate keys."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Google AI Studio — API key list with copy button"
                ),
            },
        ],
        "spark_config": [
            {
                "title": "Add a Google Gemini connection",
                "description": (
                    "In Spark, open <strong>Connections</strong> (personal mode: the first "
                    "section of <strong>Settings</strong>; a server: <strong>Account → "
                    "Connections</strong>), choose <strong>Add connection</strong> and pick "
                    "<strong>Google Gemini</strong>.  Give the connection a name."
                ),
            },
            {
                "title": "Enter the API key",
                "description": (
                    "Paste the API key you copied from Google AI Studio into <strong>API key</strong>.  The key goes straight to the secrets store and is "
                    "never shown again."
                ),
            },
            {
                "title": "Save and check its models",
                "description": (
                    "Click <strong>Save</strong>.  Spark checks the settings and then which "
                    "models the connection can reach.  Open the connection's "
                    "<strong>Models</strong> to enable the models you want to offer, then start a conversation with a Gemini model."
                ),
            },
        ],
        "troubleshooting": [
            _trouble(
                "API key not valid / 400 error",
                (
                    "Ensure the key was copied correctly with no extra spaces.  "
                    "If the key was recently created, wait a minute for it to "
                    "propagate."
                ),
            ),
            _trouble(
                "Quota exceeded (429)",
                (
                    "You have hit the free-tier rate limit.  Wait a minute before "
                    "retrying, or check your quota in the Google Cloud Console."
                ),
            ),
            _trouble(
                "Model not available in your region",
                (
                    "Some Gemini models may not be available in all regions.  "
                    "Check the Google AI documentation for regional availability."
                ),
            ),
        ],
    },
    "xai": {
        "id": "xai",
        "title": "X.AI",
        "icon": "bi-lightning",
        "summary": (
            "Connect to Grok models via the X.AI API.  This guide walks you "
            "through creating an X.AI account and generating an API key."
        ),
        "links": [
            _link("X.AI Console", "https://console.x.ai/"),
            _link("API Documentation", "https://docs.x.ai/docs/overview", _ICON_BOOK),
        ],
        "prerequisites": [
            "An X (Twitter) account or email address to create an X.AI account",
            "A payment method — the API is usage-based",
        ],
        "steps": [
            {
                "title": "Create an X.AI account",
                "description": (
                    "Visit <strong>console.x.ai</strong> and sign up.  You can "
                    "register using your X (Twitter) account or with an email address."
                ),
                "screenshot_placeholder": ("Screenshot: X.AI Console sign-up / login page"),
            },
            {
                "title": "Add billing information",
                "description": (
                    "Navigate to the billing section and add a payment method.  "
                    "API access requires active billing."
                ),
                "screenshot_placeholder": ("Screenshot: X.AI Console — Billing page"),
                "tip": (
                    "X.AI may offer free credits for new accounts.  Check "
                    "the billing dashboard for your current balance."
                ),
            },
            {
                "title": "Navigate to API Keys",
                "description": (
                    "In the X.AI Console, find the <strong>API Keys</strong> "
                    "section.  This is typically in the dashboard or under your "
                    "account settings."
                ),
                "screenshot_placeholder": ("Screenshot: X.AI Console — API Keys page"),
            },
            {
                "title": "Create a new API key",
                "description": (
                    "Click <strong>Create API Key</strong> (or similar).  Give "
                    'it a descriptive name like "Spark".  Copy the key '
                    "immediately — it is only shown once."
                ),
                "screenshot_placeholder": (
                    "Screenshot: X.AI Console — Create API Key dialog " "with generated key"
                ),
                "tip": (
                    "As with all API keys, store it securely.  You will need "
                    "to create a new key if you lose this one."
                ),
            },
        ],
        "spark_config": [
            {
                "title": "Add an X.AI connection",
                "description": (
                    "In Spark, open <strong>Connections</strong> (personal mode: the first "
                    "section of <strong>Settings</strong>; a server: <strong>Account → "
                    "Connections</strong>), choose <strong>Add connection</strong> and pick "
                    "<strong>X.AI</strong>.  Give the connection a name."
                ),
            },
            {
                "title": "Enter the API key",
                "description": (
                    "Paste the API key you copied from the X.AI Console into <strong>API key</strong>.  The key goes straight to the secrets store and is "
                    "never shown again."
                ),
            },
            {
                "title": "Save and check its models",
                "description": (
                    "Click <strong>Save</strong>.  Spark checks the settings and then which "
                    "models the connection can reach.  Open the connection's "
                    "<strong>Models</strong> to enable the models you want to offer, then start a conversation with a Grok model."
                ),
            },
        ],
        "troubleshooting": [
            _trouble(
                "Authentication error (401)",
                (
                    "Verify your API key is correct and has not been revoked.  "
                    "Ensure there are no leading or trailing spaces."
                ),
            ),
            _trouble(
                "Model not found",
                (
                    "X.AI may update available models.  Check the X.AI "
                    "documentation for the current list of supported models."
                ),
            ),
            _trouble(
                "Rate limit or billing error",
                (
                    "Ensure your billing information is current and you have "
                    "sufficient credits or an active payment method."
                ),
            ),
        ],
    },
}


def _mark_safe(guide: dict) -> dict:
    """Pre-mark HTML content fields as Markup so the template renders them safely.

    All content originates from the hardcoded PROVIDER_GUIDES dict above — there
    is no user input involved, so marking as safe is appropriate.
    """
    import copy

    from markupsafe import Markup

    g = copy.deepcopy(guide)

    for step_list_key in ("steps", "spark_config"):
        for step in g.get(step_list_key, []):
            if "description" in step:
                step["description"] = Markup(step["description"])
            if "tip" in step:
                step["tip"] = Markup(step["tip"])
            for i, sub in enumerate(step.get("substeps", [])):
                step["substeps"][i] = Markup(sub)

    for prereq_idx, prereq in enumerate(g.get("prerequisites", [])):
        g["prerequisites"][prereq_idx] = Markup(prereq)

    for item in g.get("troubleshooting", []):
        item["problem"] = Markup(item["problem"])
        item["solution"] = Markup(item["solution"])

    return g


def _connections_screen(request: Request) -> str:
    """Where connections are managed: the Connections page on a server, Settings otherwise
    (the same choice as the guide page's own "Back to Connections" link)."""
    if getattr(request.app.state, "identity_attached", False):
        return "/admin/connections"
    return "/settings#section-connections"


@router.get("/{provider_id}", response_class=HTMLResponse)
async def provider_guide(provider_id: str, request: Request) -> HTMLResponse:
    """Render a provider setup guide page."""
    guide = PROVIDER_GUIDES.get(provider_id)
    if not guide:
        return RedirectResponse(_connections_screen(request), status_code=302)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "provider_guide.html",
        {"guide": _mark_safe(guide)},
    )
