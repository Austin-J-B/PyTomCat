from tomcat.intents import classify

class Dummy:
    ch_feeding_team = 111
    ch_due_portal = 222

# You can import settings in real tests; this just sketches intent behavior.

def test_cat_show():
    intent = classify(999, "TomCat, show me Microwave")
    assert intent and intent.type == "cat_show"
