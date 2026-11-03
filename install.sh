#!/usr/bin/env bash
# Установка Wired Code в домашнюю папку (без root): ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
D="$HOME/.local/share/wired-code"

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

mkdir -p "$D/tests" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/org.kde.syntax-highlighting/themes"
cp Code.qml Editor.qml TreeNode.qml ToolTipBox.qml lsp.py runner.py icon.svg "$D/"
cp tests/editor_test.qml "$D/tests/"
# темы: если стоит рабочий стол Wired (wired-themes) — ссылка на общий файл, иначе своя копия
if [ -f "$HOME/.local/share/wired-themes/themes.js" ]; then ln -sfn ../wired-themes/themes.js "$D/themes.js"; else cp themes.js "$D/"; fi
mkdir -p "$D/qml/QMLTermWidget/color-schemes"
cp colorschemes/*.colorscheme "$D/qml/QMLTermWidget/color-schemes/"
cp -n syntax-themes/*.theme "$HOME/.local/share/org.kde.syntax-highlighting/themes/" 2>/dev/null || true
install -m 755 wired-code "$HOME/.local/bin/wired-code"
# в ярлыке полные пути: у Plasma в PATH нет ~/.local/bin
sed "s|@BIN@|$HOME/.local/bin/wired-code|; s|@ICON@|$D/icon.svg|" wired-code.desktop > "$HOME/.local/share/applications/wired-code.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Готово: запусти Wired Code из меню или командой wired-code"
