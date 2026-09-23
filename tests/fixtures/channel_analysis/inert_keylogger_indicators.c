/* Static-analysis specimen only. It never reads input or writes a log. */
/* Keep indicators as bare data, with no explanatory label in the binary. */
__attribute__((used)) static const char indicators[] =
    "GetAsyncKeyState\0"
    "SetWindowsHookExA\0"
    "/dev/input/event0\0"
    "XRecordEnableContext\0"
    "session.dat\0";

int main(void) {
    return 0;
}
