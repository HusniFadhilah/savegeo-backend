"""Regional wildfire monitoring scopes and official incident references.

References document reported incidents, not the identity of every FIRMS detection
in the regional monitoring window. Observation counts always come from the DB.
"""

EVENT_ISLANDS = {
    "all": "Indonesia",
    "kalimantan-2026": "Kalimantan",
    "sumatra-2026": "Sumatra",
    "jawa-2026": "Jawa",
    "sulawesi-2026": "Sulawesi",
    "bali-nusa-tenggara-2026": "Bali_Nusa_Tenggara",
    "maluku-2026": "Maluku",
    "papua-2026": "Papua",
}

# New scopes use the exact same cached province polygons as CAMS integration.
# This includes offshore islands and excludes neighbouring countries.
PROVINCE_MASK_EVENTS = frozenset(EVENT_ISLANDS) - {"kalimantan-2026", "sumatra-2026"}

OFFICIAL_REPORTS = {
    "jawa-2026": [{
        "title": "BNPB: karhutla Gunung Argopuro, Jawa Timur",
        "event_date": "2026-09-08",
        "location": "Cikasur, Gunung Argopuro, Jawa Timur",
        "url": "https://bnpb.go.id/index.php/berita/perkembangan-situasi-dan-penanganan-bencana-di-tanah-air-10-september-2026",
    }],
    "sulawesi-2026": [{
        "title": "BNPB: karhutla Majene, Sulawesi Barat",
        "event_date": "2026-09-19",
        "location": "Kabupaten Majene, Sulawesi Barat",
        "url": "https://bnpb.go.id/berita/perkembangan-situasi-dan-penanganan-bencana-di-tanah-air-20-september-2026",
    }],
    "bali-nusa-tenggara-2026": [{
        "title": "BNPB: penanganan karhutla Loloan, Lombok Utara",
        "event_date": "2026-09-24",
        "location": "Desa Loloan, Kecamatan Bayan, Lombok Utara, NTB (tanggal pemutakhiran)",
        "url": "https://bnpb.go.id/berita/perkembangan-situasi-dan-penanganan-bencana-di-tanah-air-per-25-september-2026",
    }],
    "maluku-2026": [{
        "title": "BNPB DIBI: karhutla Maluku Utara",
        "event_date": "2026-10-02",
        "location": "Ternate, Tidore, Taliabu, Halmahera dan Morotai (rekap BNPB 2 Oktober)",
        "url": "https://gis.bnpb.go.id/databencana/tabel/pencarian.php",
    }],
    "papua-2026": [{
        "title": "BNPB: karhutla Nabire, Papua Tengah",
        "event_date": "2026-09-19",
        "location": "Distrik Nabire dan Nabire Barat, Papua Tengah",
        "url": "https://bnpb.go.id/berita/perkembangan-situasi-dan-penanganan-bencana-di-tanah-air-20-september-2026",
    }],
}
