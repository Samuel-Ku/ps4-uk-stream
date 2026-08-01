#include <cstdio>
#include "OnscreenKeyboard.h"

int main() {
    cs::OnscreenKeyboard kb;
    kb.setText("Дю");
    kb.append(U'н');
    kb.append(U'а');
    if (kb.text() != "Дюна") { std::fprintf(stderr, "text=%s\n", kb.text().c_str()); return 1; }
    kb.backspace();
    if (kb.text() != "Дюн") { std::fprintf(stderr, "after backspace=%s\n", kb.text().c_str()); return 2; }
    kb.clear();
    if (!kb.text().empty()) { std::fprintf(stderr, "not cleared\n"); return 3; }
    return 0;
}
