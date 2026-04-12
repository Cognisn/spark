"""Provider setup guide endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/help/provider")

# ---------------------------------------------------------------------------
# Guide definitions — one per LLM provider
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
            {
                "label": "Anthropic Console",
                "url": "https://console.anthropic.com/",
                "icon": "bi-box-arrow-up-right",
            },
            {
                "label": "API Documentation",
                "url": "https://docs.anthropic.com/en/api/getting-started",
                "icon": "bi-book",
            },
            {
                "label": "Pricing",
                "url": "https://www.anthropic.com/pricing",
                "icon": "bi-currency-dollar",
            },
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
                "title": "Enable the Anthropic provider",
                "description": (
                    "In Spark, go to <strong>Settings → LLM Providers → Anthropic</strong> "
                    "and toggle <strong>Enabled</strong> on."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Spark Settings — Anthropic provider section " "with Enabled toggle"
                ),
            },
            {
                "title": "Enter your API key",
                "description": (
                    "Paste the API key you copied from the Anthropic Console into "
                    "the <strong>API Key</strong> field, then click "
                    "<strong>Save Settings</strong>."
                ),
                "screenshot_placeholder": ("Screenshot: Spark Settings — Anthropic API Key field"),
            },
            {
                "title": "Verify the connection",
                "description": (
                    "Create a new conversation and select a Claude model from the "
                    "model picker.  Send a test message to confirm everything is "
                    "working."
                ),
            },
        ],
        "troubleshooting": [
            {
                "problem": "Authentication error (401)",
                "solution": (
                    "Double-check that the API key is correct and has not been "
                    "revoked.  Ensure there are no leading/trailing spaces."
                ),
            },
            {
                "problem": "Insufficient credits / billing error",
                "solution": (
                    "Visit the Anthropic Console billing page and ensure a valid "
                    "payment method is on file and you have available credits."
                ),
            },
            {
                "problem": "Rate limit exceeded (429)",
                "solution": (
                    "You have exceeded your usage tier limits.  Wait a moment and "
                    "retry, or upgrade your usage tier in the Anthropic Console."
                ),
            },
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
            {
                "label": "AWS Console",
                "url": "https://console.aws.amazon.com/bedrock/",
                "icon": "bi-box-arrow-up-right",
            },
            {
                "label": "Bedrock Documentation",
                "url": "https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html",
                "icon": "bi-book",
            },
            {
                "label": "Bedrock Pricing",
                "url": "https://aws.amazon.com/bedrock/pricing/",
                "icon": "bi-currency-dollar",
            },
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
                    "<strong>SSO</strong> — Run <code>aws configure sso</code> in your terminal and follow the prompts.  This is the recommended method for organisations using AWS IAM Identity Centre.",
                    "<strong>IAM</strong> — Create an IAM user with Bedrock permissions and run <code>aws configure</code> to store the access key and secret key.",
                    "<strong>Session</strong> — Use temporary credentials via <code>aws sts get-session-token</code>.  Suitable for short-lived access.",
                ],
                "tip": (
                    "The IAM user or role needs at minimum the "
                    "<code>bedrock:InvokeModel</code> and "
                    "<code>bedrock:InvokeModelWithResponseStream</code> permissions."
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
                "title": "Enable the AWS Bedrock provider",
                "description": (
                    "In Spark, go to <strong>Settings → LLM Providers → AWS Bedrock</strong> "
                    "and toggle <strong>Enabled</strong> on."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Spark Settings — AWS Bedrock provider section"
                ),
            },
            {
                "title": "Set the region and auth method",
                "description": (
                    "Enter the AWS region where you enabled Bedrock model access "
                    "(e.g. <code>us-east-1</code>).  Select the authentication "
                    "method that matches how you configured your AWS CLI credentials."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Spark Settings — AWS Bedrock region and " "auth method fields"
                ),
            },
            {
                "title": "Save and verify",
                "description": (
                    "Click <strong>Save Settings</strong>, then create a new "
                    "conversation.  You should see Bedrock models in the model "
                    "picker."
                ),
            },
        ],
        "troubleshooting": [
            {
                "problem": "No models appear in the model picker",
                "solution": (
                    "Ensure you have requested and been granted access to models "
                    "in the Bedrock console.  Also verify the region in Spark "
                    "matches the region where you enabled model access."
                ),
            },
            {
                "problem": "Access denied / credential errors",
                "solution": (
                    "Run <code>aws sts get-caller-identity</code> to verify your "
                    "credentials are valid.  For SSO, you may need to run "
                    "<code>aws sso login</code> to refresh your session."
                ),
            },
            {
                "problem": "Timeout connecting to Bedrock",
                "solution": (
                    "Check your network connection and ensure outbound HTTPS "
                    "traffic to AWS endpoints is not blocked by a firewall or proxy."
                ),
            },
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
            {
                "label": "Ollama Website",
                "url": "https://ollama.com/",
                "icon": "bi-box-arrow-up-right",
            },
            {
                "label": "Model Library",
                "url": "https://ollama.com/library",
                "icon": "bi-collection",
            },
            {
                "label": "GitHub",
                "url": "https://github.com/ollama/ollama",
                "icon": "bi-github",
            },
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
                "title": "Enable the Ollama provider",
                "description": (
                    "In Spark, go to <strong>Settings → LLM Providers → Ollama</strong> "
                    "and toggle <strong>Enabled</strong> on."
                ),
                "screenshot_placeholder": ("Screenshot: Spark Settings — Ollama provider section"),
            },
            {
                "title": "Set the base URL",
                "description": (
                    "The default URL is <code>http://localhost:11434</code>, which "
                    "is correct for a standard local installation.  Only change "
                    "this if Ollama is running on a different machine or port."
                ),
            },
            {
                "title": "Save and verify",
                "description": (
                    "Click <strong>Save Settings</strong>, then create a new "
                    "conversation.  Your locally downloaded models should appear "
                    "in the model picker."
                ),
            },
        ],
        "troubleshooting": [
            {
                "problem": "Connection refused / cannot reach Ollama",
                "solution": (
                    "Ensure the Ollama application is running.  On macOS, check "
                    "for the Ollama icon in the menu bar.  You can also start it "
                    "manually with <code>ollama serve</code>."
                ),
            },
            {
                "problem": "No models appear in the model picker",
                "solution": (
                    "You need to pull at least one model first.  Run "
                    "<code>ollama pull llama3.3</code> (or another model) in your "
                    "terminal."
                ),
            },
            {
                "problem": "Model runs very slowly",
                "solution": (
                    "Larger models require more RAM and compute.  Try a smaller "
                    "model variant, or check that no other heavy applications are "
                    "consuming system resources."
                ),
            },
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
            {
                "label": "Google AI Studio",
                "url": "https://aistudio.google.com/",
                "icon": "bi-box-arrow-up-right",
            },
            {
                "label": "API Documentation",
                "url": "https://ai.google.dev/gemini-api/docs",
                "icon": "bi-book",
            },
            {
                "label": "Pricing",
                "url": "https://ai.google.dev/gemini-api/docs/pricing",
                "icon": "bi-currency-dollar",
            },
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
                "title": "Enable the Google Gemini provider",
                "description": (
                    "In Spark, go to <strong>Settings → LLM Providers → Google Gemini</strong> "
                    "and toggle <strong>Enabled</strong> on."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Spark Settings — Google Gemini provider section"
                ),
            },
            {
                "title": "Enter your API key",
                "description": (
                    "Paste the API key from Google AI Studio into the "
                    "<strong>API Key</strong> field, then click "
                    "<strong>Save Settings</strong>."
                ),
                "screenshot_placeholder": (
                    "Screenshot: Spark Settings — Google Gemini API Key field"
                ),
            },
            {
                "title": "Verify the connection",
                "description": (
                    "Create a new conversation and select a Gemini model from the "
                    "model picker.  Send a test message to confirm everything is "
                    "working."
                ),
            },
        ],
        "troubleshooting": [
            {
                "problem": "API key not valid / 400 error",
                "solution": (
                    "Ensure the key was copied correctly with no extra spaces.  "
                    "If the key was recently created, wait a minute for it to "
                    "propagate."
                ),
            },
            {
                "problem": "Quota exceeded (429)",
                "solution": (
                    "You have hit the free-tier rate limit.  Wait a minute before "
                    "retrying, or check your quota in the Google Cloud Console."
                ),
            },
            {
                "problem": "Model not available in your region",
                "solution": (
                    "Some Gemini models may not be available in all regions.  "
                    "Check the Google AI documentation for regional availability."
                ),
            },
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
            {
                "label": "X.AI Console",
                "url": "https://console.x.ai/",
                "icon": "bi-box-arrow-up-right",
            },
            {
                "label": "API Documentation",
                "url": "https://docs.x.ai/docs/overview",
                "icon": "bi-book",
            },
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
                "title": "Enable the X.AI provider",
                "description": (
                    "In Spark, go to <strong>Settings → LLM Providers → X.AI</strong> "
                    "and toggle <strong>Enabled</strong> on."
                ),
                "screenshot_placeholder": ("Screenshot: Spark Settings — X.AI provider section"),
            },
            {
                "title": "Enter your API key",
                "description": (
                    "Paste the API key from the X.AI Console into the "
                    "<strong>API Key</strong> field, then click "
                    "<strong>Save Settings</strong>."
                ),
                "screenshot_placeholder": ("Screenshot: Spark Settings — X.AI API Key field"),
            },
            {
                "title": "Verify the connection",
                "description": (
                    "Create a new conversation and select a Grok model from the "
                    "model picker.  Send a test message to confirm everything is "
                    "working."
                ),
            },
        ],
        "troubleshooting": [
            {
                "problem": "Authentication error (401)",
                "solution": (
                    "Verify your API key is correct and has not been revoked.  "
                    "Ensure there are no leading or trailing spaces."
                ),
            },
            {
                "problem": "Model not found",
                "solution": (
                    "X.AI may update available models.  Check the X.AI "
                    "documentation for the current list of supported models."
                ),
            },
            {
                "problem": "Rate limit or billing error",
                "solution": (
                    "Ensure your billing information is current and you have "
                    "sufficient credits or an active payment method."
                ),
            },
        ],
    },
}


@router.get("/{provider_id}", response_class=HTMLResponse)
async def provider_guide(provider_id: str, request: Request) -> HTMLResponse:
    """Render a provider setup guide page."""
    guide = PROVIDER_GUIDES.get(provider_id)
    if not guide:
        return RedirectResponse("/settings#section-providers", status_code=302)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "provider_guide.html",
        {"guide": guide},
    )
