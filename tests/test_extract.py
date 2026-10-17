from astrbot_plugin_virtual_body.core import extract

from tests.test_space import MANOR


def test_extract_her_move():
    assert extract.extract_her_move("她起身去厨房热牛奶", MANOR) == {"room": "kitchen", "verb_at": 3, "word": "厨房"}
    assert extract.extract_her_move("她说她要回书房拿书", MANOR)["room"] == "study"


def test_extract_her_move_negation_and_quote():
    assert extract.extract_her_move("她别去书房了", MANOR) is None
    assert extract.extract_her_move('她说"去厨房看看"，其实没动', MANOR) is None


def test_extract_her_move_no_subject():
    assert extract.extract_her_move("去厨房吧", MANOR) is None    # 无"她"主语


def test_extract_him_move_and_direct():
    assert extract.extract_him("我去客厅找你", MANOR) == {"room": "living", "direct": False}
    assert extract.extract_him("我在书房等你", MANOR) == {"room": "study", "direct": True}
    assert extract.extract_him("今天天气不错", MANOR) is None


def test_extract_him_negation():
    assert extract.extract_him("别去厨房了", MANOR) is None
    assert extract.extract_him("我不在书房", MANOR) is None


def test_room_word_longest_wins():
    # "主卧"与"卧"（若有短别名）长词占优；"厨房"与"菜园"不互相误触
    assert extract.extract_him("我在主卧", MANOR)["room"] == "master-bed"


def test_room_mention_order_and_quotes():
    ms = extract.extract_room_mentions('先去厨房，再去"书房"', MANOR)
    rooms = [m["room"] for m in ms]
    assert rooms[0] == "kitchen"
    study = [m for m in ms if m["room"] == "study"][0]
    assert study["quoted"] is True


def test_text_has_room_word_gate():
    assert extract.text_has_room_word("书房门没关", MANOR) is True
    assert extract.text_has_room_word('所谓"书房"只是比喻', MANOR) is False
    assert extract.text_has_room_word("今天好累", MANOR) is False
