# Shopaholic EasyBuyer

## Project overview

Online shoppers can spend a long time comparing products, reading reviews, and checking prices. This creates decision fatigue and makes it harder to choose confidently, especially for people who are unfamiliar with a product category. **Shopaholic EasyBuyer** is a shopping assistant prototype for frequent online shoppers, with a particular focus on everyday consumers aged 20–40 who want to save time and get help evaluating unfamiliar products. It operates in the e-commerce and consumer-retail domain.

The user describes what they want in natural language and may include a budget. When configured, OpenRouter's foundation model parses the request and BuyWhere supplies live product listings. The app applies local relevance, availability, link, and budget checks, then returns up to five recommendations with product-page links. A 200-row CSV snapshot is available when live results are unavailable. The user can accept a recommendation or reject it with a reason. The prototype supports shopping decisions; it does not place orders or complete purchases. Product recommendations come from retrieved catalog records rather than being invented as free-form model responses.

The project uses a hybrid build-and-buy approach: the Flask application, interface, filtering, and feedback flow are implemented in this project; intent parsing and live product-catalog search are provided by external APIs. The broader project goal is to make product discovery faster and more confident. 

## Requirements

- Python 3.9 or newer
- An OpenRouter API key for AI intent parsing (optional; the app has a local fallback)
- A BuyWhere API key for live product search (optional; the app can use the included CSV snapshot)
- Internet access for API calls, currency conversion, and product-page checks


## Setup

Open a terminal in the project directory:

```bash
cd YOUR_PROJECT_PATH
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install Flask requests
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Configure API keys

Set the keys in the same terminal session that will run the app. Replace the example values with your own keys:

```bash
export OPENROUTER_API_KEY="your_openrouter_key"
export BUYWHERE_API_KEY="your_buywhere_key"
export OPENROUTER_MODEL="openai/gpt-5-mini"
```

`OPENROUTER_MODEL` is optional; the default is `openai/gpt-5-mini`. Do not put real API keys in this README or commit them to source control.

On Windows PowerShell, use:

```powershell
$env:OPENROUTER_API_KEY = "your_openrouter_key"
$env:BUYWHERE_API_KEY = "your_buywhere_key"
$env:OPENROUTER_MODEL = "openai/gpt-5-mini"
```

The two API keys serve different purposes:

- **OpenRouter** parses the request into a product search term and budget. If this key is missing or the call fails, the app falls back to local intent parsing.
- **BuyWhere** supplies live product results. If this key is missing or no usable live results are returned, the app searches the included CSV snapshot.

## Run the app

From the project directory, with the virtual environment active and keys configured:

```bash
python shopaholic_easybuyer.py
```

Open [http://127.0.0.1:5001](http://127.0.0.1:5001) in a browser. Stop the server with `Control+C` in the terminal.


## Using the app

Enter a product request in the search box. Examples:

- `floral dress`
- `pencil`
- `chocolate`
- `knife under 300sgd`

The app displays up to five recommendations, with product name, price, and a **View Product** link. Use **Accept** or **Reject** to record feedback. A rejection requires a reason.


## Evaluation scope

The evaluation focuses on whether EasyBuyer can provide relevant, constraint-compliant, and trustworthy product recommendations for shopping decision support. It does not measure completed purchases or conversion rates.

The evaluation covers the following aspects:

**Intent and constraint understanding**

Evaluate whether the system correctly identifies the requested product and user constraints, such as budget and currency, from natural-language queries.

**Recommendation relevance**

Measure whether the returned products match the user's requested product category and intended shopping need. Relevance is evaluated independently from whether the retrieved product information is accurately represented.

**Constraint satisfaction**

Check whether recommendations satisfy explicit constraints, particularly price limits and currency requirements. For example, products returned for a query such as `knife under 300sgd` should not exceed the specified budget.

**Product link validity**

Check whether recommended product links lead to accessible and specific product pages rather than broken links, generic store pages, search-result pages, or explicitly unavailable products.

**Recommendation faithfulness**

Check whether product names, prices, and other displayed information are consistent with the retrieved catalog records. A recommendation can be faithful to the retrieved data while still being irrelevant to the user's request, so these two aspects are evaluated separately.

**Efficiency and cost**

Report backend response time and estimated OpenRouter cost per returned recommendation. The API provides `response_time_ms`; the cost is based on token usage and configured rates, not the total cost of every external service. Cost per recommendation is the request's estimated OpenRouter cost divided by the number of recommendations returned; report it as N/A when no products are returned.

## Data and evaluation materials

- [`docs/PRODUCT_DOCUMENTATION.md`](docs/PRODUCT_DOCUMENTATION.md) describes the target persona, input/output, architecture, and targeted versus reached metrics.
- [`data/products_snapshot.csv`](data/products_snapshot.csv) is the fallback product snapshot. [`data/README.md`](data/README.md) describes its contents, product sources, and refresh behavior.
- [`evals/README.md`](evals/README.md) explains the evaluation protocol, metric definitions, and what evidence is currently available.
- [`evals/evaluation_log_template.csv`](evals/evaluation_log_template.csv) is one combined blank log for queries, blinded outputs, tester ratings, and cost. It is a template, not completed evaluation results.
- `evals/evaluation_runs.jsonl` is created only when `EVALUATION_LOG_ENABLED=1`; it appends recommendation outputs and runtime metrics for evaluation sessions.

The product snapshot and evaluation evidence should be kept with the project so another reviewer can inspect what data and test cases produced the reported results. Do not present the interaction feedback log as a benchmark or as blind evaluation.


## Data handling and privacy

The project statement identifies Singapore's PDPA and China's PIPL as privacy considerations. In the current implementation, when the corresponding APIs are configured, the query is sent to OpenRouter for intent parsing and to BuyWhere for product search; accept/reject feedback is appended locally to `shopping_feedback.jsonl`. Do not submit sensitive personal information. A privacy and data-residency review is needed before deployment beyond this local prototype.

## Data files

- `data/products_snapshot.csv` — included fallback catalog (200 rows). The current snapshot schema is `id`, `name`, `category`, `platform`, `price`, `currency`, `rating`, `image`, `seller`, and `url`.
- `shopping_feedback.jsonl` — feedback records, one JSON object per line. `accepted` is `true` or `false`; rejected items also include the user's reason.

To view the full feedback records, open `shopping_feedback.jsonl` in a text editor.

## FAQ

- **Port 5001 is already in use** — stop the previous Flask process with `Control+C`, or start on another port with `PORT=5002 python shopaholic_easybuyer.py` and open `http://127.0.0.1:5002`.
- **API key shows as missing** — export it in the same terminal session before starting Python. Restart the server after changing environment variables.
