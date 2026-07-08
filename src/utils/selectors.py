SELECTORS = {
    "results_container": [
        '[role="feed"]',
        'div[aria-label*="Resultados"]',
    ],
    "result_cards": [
        '[role="feed"] > div > div > a',
        'a[aria-label][href*="/maps/place/"]',
    ],
    "business_name": [
        '[aria-label]',
        'div.qBF1Pd',
        'span.fontHeadlineSmall',
    ],
    "business_category": [
        'div.W4Efsd > span:last-child',
    ],
    "rating": [
        'span[aria-label*="estrellas"]',
        'span.MW4etd',
    ],
    "place_url": [
        'a[href*="/maps/place/"]',
    ],
    "consent_button": [
        'button[aria-label*="Aceptar todo"]',
        'button[aria-label*="Rechazar todo"]',
    ],
    "end_of_results": [
        'div:has-text("Ya llegaste al final")',
        'p[aria-label*="final"]',
    ],
}
