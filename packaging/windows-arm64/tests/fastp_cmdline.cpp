#include "cmdline.h"
#include <cassert>

#if defined(_WIN32) && (!defined(_M_ARM64) || defined(_M_ARM64EC))
#error This Windows fixture requires native ARM64
#endif

int main()
{
    cmdline::parser parser;
    parser.add<int>("threads", 't', "thread count", false, 1, cmdline::range(1, 64));
    const char *valid[] = {"fastp", "--threads", "4"};
    assert(parser.parse(3, valid));
    assert(parser.get<int>("threads") == 4);
    assert(!cmdline::detail::readable_typename<int>().empty());
    const char *invalid[] = {"fastp", "--threads", "not-a-number"};
    assert(!parser.parse(3, invalid));
    assert(parser.error().find("threads") != std::string::npos);
    return 0;
}
