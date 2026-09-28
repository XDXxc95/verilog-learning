// C++ 编译链功能自检用的探针文件 —— 不是课程材料，是体检样本。
//
// 为什么需要真编译一次：机器上「有 g++」和「有能编 verilator 生成代码的 g++」
// 是两件事。本机唯一的 g++ 是 Qt 自带的 mingw 7.3（2017 年），
// 而 verilator 是按 GCC 15.2.1 配置的 —— 它加的 -fcf-protection=none
// 在 GCC 8 才有，7.3 直接报 unrecognized command line option。
//
// 所以自检不能只看 `g++ --version` 打不打得出来，必须拿 verilator 真正会用的
// 那组 flag 编一次。这里用 -fsyntax-only：足以触发 flag 兼容性问题，
// 又不会在磁盘上留下产物、不需要指定输出路径。
//
// 注意这只是**最低门槛**，不等于整条链通了 —— 链接阶段还可能缺 -lpsapi、
// 缺 -mconsole（本机实测都撞过）。端到端的验证要靠 toolchain.cc 的接入代码，
// 那一块本仓库还没写。
int main() { return 0; }
