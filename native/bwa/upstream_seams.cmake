# Generate copies of pinned upstream sources with narrow Metal seams. Each seam
# is inert unless the Metal backend is enabled, so the CPU path stays upstream.
function(seam_replace variable before after)
    string(FIND "${${variable}}" "${before}" position)
    if(position EQUAL -1)
        message(FATAL_ERROR "Unexpected upstream BWA source: ${before}")
    endif()
    string(REPLACE "${before}" "${after}" patched "${${variable}}")
    set(${variable} "${patched}" PARENT_SCOPE)
endfunction()

set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in")
file(READ "${bwa_SOURCE_DIR}/bwamem.c" mem_source)
seam_replace(mem_source "mem_collect_intv(opt, bwt, len, seq, aux);"
    "if (!wgse_metal_take(seq, &aux->mem)) mem_collect_intv(opt, bwt, len, seq, aux);")
seam_replace(mem_source
    "kt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);"
    "if (!wgse_metal_map(opt, bwt, n, seqs, worker1, &w))\n\t\tkt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in" oracle_source)
file(WRITE "${generated}/bwamem.c"
    "#include \"metal_backend.h\"\n${mem_source}\n${oracle_source}")

# Metal shares the BWT in place, which needs page-aligned storage.
file(READ "${bwa_SOURCE_DIR}/bwt.c" bwt_source)
seam_replace(bwt_source "calloc(bwt->bwt_size, 4)"
    "(wgse_metal_enabled() ? wgse_metal_calloc(bwt->bwt_size, 4) : calloc(bwt->bwt_size, 4))")
file(WRITE "${generated}/bwt.c" "#include \"metal_backend.h\"\n${bwt_source}")

file(READ "${bwa_SOURCE_DIR}/main.c" main_source)
seam_replace(main_source "int main(int argc, char *argv[])"
    "int wgse_bwa_main(int argc, char *argv[])")
# Keep upstream's "[main] Version/CMD/Real time" stderr lines.
seam_replace(main_source "__func__" "\"main\"")
file(WRITE "${generated}/main.c" "${main_source}")
set(patched_sources bwamem bwt main)
