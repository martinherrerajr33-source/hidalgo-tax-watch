"""
Hidalgo Distress Watch - public-records signals that point at motivated sellers.

Pulls recorded documents (County Clerk, hidalgo.tx.publicsearch.us) and court
cases (Odyssey, pa.co.hidalgo.tx.us), matches each one to an HCAD parcel, and
ranks parcels 0-100 by how many distress signals are stacked on them.

Modules:
    clerk.py  - recorded documents scraper (needs a real browser: Playwright)
    court.py  - court case scraper (plain HTTP, ASP.NET form posts)
    cad.py    - HCAD / TrueProdigy API client + record-to-parcel matching
    names.py  - name / legal-description normalization helpers
    score.py  - signal categories and the 0-100 distress score
    store.py  - JSON data files kept in the repo between runs
"""
