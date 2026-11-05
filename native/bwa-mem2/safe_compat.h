#pragma once

#include <string.h>

// Darwin's memset_s declaration has a different destination qualifier.
#define memset_s wgse_memset_s
