#!/usr/bin/env bash
# Установка D3R Code в домашнюю папку (без root): ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
D="$HOME/.local/share/d3r-code"

need() { command -v "$1" >/dev/null || miss+=("$2"); }
miss=()
[ -x /usr/lib/qt6/bin/qml ] || miss+=("qt6-declarative")
[ -e /usr/lib/qt6/qml/QMLTermWidget/qmldir ] || miss+=("qmltermwidget")
[ -d /usr/lib/qt6/qml/org/kde/syntaxhighlighting ] || miss+=("syntax-highlighting")
need python3 python; need g++ gcc
if ((${#miss[@]})); then echo "Не хватает пакетов: ${miss[*]}"; echo "  Arch/CachyOS: sudo pacman -S ${miss[*]}"; exit 1; fi
command -v clangd >/dev/null || echo "clangd не найден — автодополнения C++ не будет (sudo pacman -S clang)"
python3 -c "import jedi" 2>/dev/null || echo "jedi не найден — автодополнения Python не будет (sudo pacman -S python-jedi)"
command -v clang-format >/dev/null || echo "clang-format не найден — форматирования C++ не будет (sudo pacman -S clang)"
python3 -c "import black" 2>/dev/null || command -v black >/dev/null || echo "black не найден — форматирования Python не будет (sudo pacman -S python-black)"
command -v cmake >/dev/null || echo "cmake не найден — проекты C++ с CMakeLists.txt не соберутся (sudo pacman -S cmake)"

mkdir -p "$D/tests" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/org.kde.syntax-highlighting/themes" "$HOME/.local/share/org.kde.syntax-highlighting/syntax"
cp Code.qml Editor.qml TreeNode.qml ToolTipBox.qml lsp.py runner.py icon.svg "$D/"
cp tests/editor_test.qml "$D/tests/"
# темы: если стоит рабочий стол D3R (d3r-themes) — ссылка на общий файл, иначе своя копия
if [ -f "$HOME/.local/share/d3r-themes/themes.js" ]; then ln -sfn ../d3r-themes/themes.js "$D/themes.js"; else cp themes.js "$D/"; fi
mkdir -p "$D/qml/QMLTermWidget/color-schemes"
cp colorschemes/*.colorscheme "$D/qml/QMLTermWidget/color-schemes/"
cp -n syntax-themes/*.theme "$HOME/.local/share/org.kde.syntax-highlighting/themes/" 2>/dev/null || true
cp syntax/*.xml "$HOME/.local/share/org.kde.syntax-highlighting/syntax/"   # C++ как в VS: функции, переменные, типы, макросы
install -m 755 d3r-code "$HOME/.local/bin/d3r-code"
# в ярлыке полные пути: у Plasma в PATH нет ~/.local/bin
sed "s|@BIN@|$HOME/.local/bin/d3r-code|; s|@ICON@|$D/icon.svg|" d3r-code.desktop > "$HOME/.local/share/applications/d3r-code.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Готово: запусти D3R Code из меню или командой d3r-code"
