// Самопроверка правок в Editor.qml без окна: QT_QPA_PLATFORM=offscreen qml tests/editor_test.qml
import QtQuick
import ".."

Item {
    width: 800; height: 600
    QtObject { id: cfgM; property string font: "monospace"; property int fontSize: 14; property int tabWidth: 4; property bool wrap: false
               property bool lineNumbers: true; property bool curLine: true; property bool autoClose: true; property bool anim: false }
    Item { id: palM; property color bg: "#000"; property color panel: "#111"; property color head: "#222"; property color btn: "#333"; property color text: "#eee"
           property color dim: "#888"; property color acc: "#68f"; property color light: "#9bf"; property color sel: "#35e"; property color faint: "#666" }
    QtObject { id: appM; property string mono: "monospace"; property var cfg: cfgM; property var pal: palM; property var th: ({ scheme: "Wired" }); function zoom(d) {} }
    Editor { id: ed; app: appM; title: "t.cpp"; anchors.fill: parent }

    property int fails: 0
    function eq(name, got, want) { if (got !== want) { fails++; console.log("FAIL", name, JSON.stringify(got), "!=", JSON.stringify(want)); } }
    function press(key, text, mods) { const e = { key: key, text: text || "", modifiers: mods || 0, accepted: false }; ed.key(e); if (!e.accepted && text) ed.area.insert(ed.area.cursorPosition, text); }
    function set(t, pos) { ed.area.text = t; ed.area.cursorPosition = pos === undefined ? t.length : pos; }

    Component.onCompleted: Qt.callLater(run)   // после того как редактор сам закончит создаваться
    function run() {
        // Enter после { — отступ и закрывающая скобка на своей строке
        set("int main() {}", 12); press(Qt.Key_Return);
        eq("enter-brace", ed.area.text, "int main() {\n    \n}"); eq("enter-brace-cur", ed.area.cursorPosition, 17);
        // автоскобки и перешагивание закрывающей
        set("f", 1); press(0, "("); eq("autoclose", ed.area.text, "f()"); press(0, ")"); eq("skip-close", ed.area.text, "f()"); eq("skip-pos", ed.area.cursorPosition, 3);
        // Backspace между пустой парой
        set("x[]", 2); press(Qt.Key_Backspace); eq("bs-pair", ed.area.text, "x");
        // кавычка после буквы не удваивается (don't)
        set("don", 3); press(0, "'"); eq("quote-word", ed.area.text, "don'");
        // комментарий: включить и выключить
        set("a\n  b", 0); ed.area.select(0, 5); ed.toggleComment(); eq("comment-on", ed.area.text, "// a\n//   b");
        ed.area.select(0, ed.area.text.length); ed.toggleComment(); eq("comment-off", ed.area.text, "a\n  b");
        // отступ блока и обратно
        set("a\nb", 0); ed.area.select(0, 3); ed.indent(1); eq("indent", ed.area.text, "    a\n    b"); ed.indent(-1); eq("unindent", ed.area.text, "a\nb");
        // перенос строк
        set("1\n2\n3", 2); ed.moveLines(-1); eq("move-up", ed.area.text, "2\n1\n3"); ed.moveLines(1); ed.moveLines(1); eq("move-down", ed.area.text, "1\n3\n2");
        // дублировать и удалить строку
        set("ab\ncd", 1); ed.duplicate(); eq("dup", ed.area.text, "ab\nab\ncd"); ed.deleteLine(); eq("del", ed.area.text, "ab\ncd");
        set("ab\ncd", 4); ed.deleteLine(); eq("del-last", ed.area.text, "ab");
        // счётчик строк и позиция курсора
        set("a\nbb\nccc", 5); eq("lines", ed.lineCount, 3); eq("line", ed.curLine, 3); eq("col", ed.curCol, 1);
        // поиск и замена
        set("foo bar foo", 0); ed.query = "foo"; eq("matches", ed.matches.length, 2); eq("replace-all", ed.replaceAll("x"), 2); eq("replaced", ed.area.text, "x bar x");
        // парная скобка
        set("(a[b])", 0); ed.matchBracket(); eq("bracket", JSON.stringify(ed.brackets), "[0,5]");
        // Python: Enter после двоеточия — отступ, после return — на уровень левее
        ed.title = "t.py"; set("if x:", 5); press(Qt.Key_Return); eq("py-colon", ed.area.text, "if x:\n    ");
        set("    return 1", 12); press(Qt.Key_Return); eq("py-return", ed.area.text, "    return 1\n");
        // автодополнение: метод с параметрами → name(|), без параметров → name()|
        ed.title = "t.cpp"; set("v.pu", 4);
        ed.comps = [{ label: "push_back(const T &x)", insert: "push_back", kind: 2, detail: "void", doc: "", start: 2 }]; ed.compStart = 2;
        ed.acceptComp(); eq("comp-call", ed.area.text, "v.push_back()"); eq("comp-cur", ed.area.cursorPosition, 12);
        set("v.si", 4); ed.comps = [{ label: "size() const", insert: "size", kind: 2, detail: "", doc: "", start: 2 }]; ed.compStart = 2;
        ed.acceptComp(); eq("comp-noargs", ed.area.cursorPosition, 8); eq("comp-closed", ed.compStart, -1);
        set("x.pu", 4); ed.comps = [{ label: "pub", insert: "pub", kind: 5 }, { label: "push", insert: "push", kind: 6 }, { label: "apush", insert: "apush", kind: 6 }];
        ed.compStart = 2; eq("comp-filter", ed.compShown.map(c => c.insert).join(","), "pub,push,apush");
        ed.area.insert(4, "s"); eq("comp-narrow", ed.compShown.map(c => c.insert).join(","), "push,apush");
        // сохранение: запись + сверка чтением (раньше чтение обгоняло запись и сообщало ложную ошибку)
        ed.path = "/tmp/wired-code-save-test.py"; ed.loaded = true; set("print('какащке')\n".repeat(5000));
        ed.save(ok => {
            eq("save-ok", ok, true); eq("save-clean", ed.dirty, false)
            console.log(fails ? "EDITOR TEST FAILED: " + fails : "EDITOR TEST OK");
            Qt.exit(fails ? 1 : 0);
        });
    }
}
