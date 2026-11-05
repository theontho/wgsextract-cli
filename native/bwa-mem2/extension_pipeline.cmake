# Each side's SIMD8 and SIMD16 regions are disjoint, but upstream shares sort
# scratch and pads across their boundary. Early SIMD8 work needs private buffers.
function(wgse_pipeline_side side marker16 marker8 end_marker)
    string(FIND "${bwamem_source}" "${marker16}" begin16)
    string(FIND "${bwamem_source}" "${marker8}" begin8)
    if(begin16 LESS 0 OR begin8 LESS_EQUAL begin16)
        message(FATAL_ERROR "Unexpected upstream ${side} extension groups")
    endif()
    string(SUBSTRING "${bwamem_source}" ${begin8} -1 tail)
    string(FIND "${tail}" "${end_marker}" length8)
    if(length8 LESS 0)
        message(FATAL_ERROR "Unexpected upstream ${side} extension boundary")
    endif()
    string(SUBSTRING "${tail}" 0 ${length8} body)
    string(SUBSTRING "${tail}" ${length8} -1 after)
    string(REPLACE "pair_ar = seqPairArray${side}128;"
        "SeqPair *pair_ar = buffers.pairs;" body "${body}")
    string(REPLACE "pair_ar_aux = seqPairArrayAux;"
        "SeqPair *pair_ar_aux = buffers.aux;" body "${body}")
    string(REPLACE "nump = numPairs${side}128;"
        "int nump = numPairs${side}128;" body "${body}")
    string(REPLACE "for ( i=0;" "for (int i=0;" body "${body}")
    string(REPLACE "sortPairsLen(pair_ar, nump, seqPairArrayAux, hist);"
        "sortPairsLen(pair_ar, nump, buffers.aux, buffers.hist);" body "${body}")
    string(SUBSTRING "${bwamem_source}" 0 ${begin16} before)
    math(EXPR length16 "${begin8} - ${begin16}")
    string(SUBSTRING "${bwamem_source}" ${begin16} ${length16} group16)
    set(work "
    WgseExtensionWork wgse_${side}_work([&](bool independent) {
        auto buffers = independent
            ? wgse_extension_scratch(seqPairArray${side}128, numPairs${side}128,
                MAX_SEQ_LEN8 + MAX_SEQ_LEN16 + 32)
            : WgseExtensionScratch{seqPairArray${side}128, seqPairArrayAux, hist};
${body}
    });
")
    set(bwamem_source
        "${before}${work}${group16}\n    wgse_${side}_work.finish();\n${after}"
        PARENT_SCOPE)
endfunction()

wgse_pipeline_side(Left
    "    //****************** Left - vector int16"
    "    //****************** Left - vector int8"
    "    // tprof[CLEFT][tid]")
wgse_pipeline_side(Right
    "    // ************************* Right - vector int16"
    "    // ************************* Right, vector int8"
    "    _mm_free(hist);")
