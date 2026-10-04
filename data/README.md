# Product Snapshot Data

## What the CSV contains

[`products_snapshot.csv`](products_snapshot.csv) is EasyBuyer's local catalog snapshot. It contains **200 product listings**, with IDs numbered 1–200. Each row stores:

| Column | Contents |
|---|---|
| `id` | Snapshot record ID |
| `name` | Product title |
| `category` | Product category used by local filtering |
| `platform` | Marketplace or source label supplied for the listing |
| `price` / `currency` | Recorded listing price and currency |
| `rating` | Rating when present in the source record |
| `image` | Product image URL when present |
| `seller` | Seller or merchant label |
| `url` | Product purchase-page URL |

The catalog covers 14 categories: earbuds, phones, laptops, shoes, water bottles, backpacks, headphones, small appliances, home, beauty, fashion, kitchen, toys, and pet supplies. The first 13 categories have 15 listings each; pet supplies has 5. Recorded currencies are USD (136 listings), SGD (58), and PKR (6).

## Product sources

Each listing's `url` points to its product page and is the record-level source for the product. The `platform` and `seller` fields provide source and merchant labels. The current snapshot contains listings across **50 URL hostnames**. The most represented hosts are:

| Product-page host | Listings |
|---|---:|
| `joesge.myshopify.com` | 23 |
| `www.amazon.sg` | 18 |
| `www.harveynorman.com.sg` | 17 |
| `decathlon.com` | 15 |
| `3leggedthing.myshopify.com` | 10 |
| `ourbarehands.com` | 10 |
| `constructiveplaythings.myshopify.com` | 9 |
| `www.amazon.com` | 8 |

For live searches, the application queries the BuyWhere API when `BUYWHERE_API_KEY` is configured. The CSV is the local fallback used when live retrieval is unavailable or returns no usable recommendations; it is not refreshed automatically when the application starts.

## How the app uses the columns

The application reads this CSV by default. It normalizes the rows, matches product names/categories against the query, applies budget and currency checks, validates candidate product pages, and returns up to five recommendations. `platform`, `seller`, `rating`, and `image` enrich the result; `url` is the purchase link shown to the shopper.

## Refreshing the snapshot

With a valid `BUYWHERE_API_KEY`, run:

```bash
python shopaholic_easybuyer.py --refresh-snapshot
```

The script writes a new timestamped `products_snapshot_live_*.csv` file in this directory and leaves `products_snapshot.csv` unchanged. To run the app against a refreshed file, set `PRODUCTS_CSV` to its path when starting the server.
