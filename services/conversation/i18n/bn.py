"""Bengali spoken system strings (Bengali script). Machine-drafted: have a native speaker review before production."""

STRINGS: dict[str, str] = {
    "fallback_goodbye": "ধন্যবাদ, নমস্কার।",
    "max_duration_goodbye": "এই কলের সময় শেষ হয়ে গেছে। কল করার জন্য ধন্যবাদ, নমস্কার।",
    "fallback_llm_error": "দুঃখিত, এখন একটু সমস্যা হচ্ছে। আবার একবার বলবেন?",
    "end_call_check": "আর কিছু সাহায্য লাগবে?",
    "transfer_failed_fallback": "দুঃখিত, এখন আপনাকে কোনো প্রতিনিধির সাথে যুক্ত করা গেল না।",
    "callflow_dial_refused": "এই মুহূর্তে কলটি ট্রান্সফার করা যাচ্ছে না। অনুগ্রহ করে পরে আবার চেষ্টা করুন। নমস্কার।",
    "booking_fabrication_transfer_announcement": (
        "আপনার বুকিং ঠিকভাবে হয়েছে কি না নিশ্চিত করতে আমাদের টিমের একজন দেখে নিচ্ছেন — এক মুহূর্ত।"
    ),
}

TOOL_FILLERS: tuple[tuple[str, float], ...] = (
    ("এক মুহূর্ত।", 1.0),
    ("এক সেকেন্ড।", 1.0),
    ("আচ্ছা, এখনই দেখছি।", 1.5),
    ("একটু অপেক্ষা করুন।", 1.5),
    ("নিশ্চয়ই, এখনই দেখে নিচ্ছি।", 2.0),
    ("এক মুহূর্ত অপেক্ষা করুন, এখনই শেষ করছি।", 2.4),
)

FIRST_TURN_FILLERS: tuple[tuple[str, float], ...] = (
    ("আচ্ছা, এক মুহূর্ত।", 1.0),
    ("আচ্ছা, এক সেকেন্ড।", 1.0),
    ("আচ্ছা, একটু অপেক্ষা করুন।", 1.5),
    ("আচ্ছা, এক মুহূর্ত দিন।", 1.5),
    ("আচ্ছা, অনুগ্রহ করে এক মুহূর্ত অপেক্ষা করুন।", 2.0),
    ("আচ্ছা, অনুগ্রহ করে একটু অপেক্ষা করুন।", 2.4),
)

# "নমস্কার" opens calls too, so it is a farewell only, not a closure.
CLOSURE_PHRASES: tuple[str, ...] = ("এটুকুই", "আর কিছু না", "ধন্যবাদ", "বাই", "ব্যস")
FAREWELL_PHRASES: tuple[str, ...] = ("নমস্কার", "আবার কথা হবে", "ভালো থাকবেন", "শুভ দিন", "কল করার জন্য ধন্যবাদ")
