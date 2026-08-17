Reverts the manifest validator's license check (#407).

`check_unfinished_placeholders` no longer flags a README still carrying the
`MINDS_TEMPLATE_LICENSE` token, because the publish flow no longer writes that
placeholder. Its other checks -- the unreplaced FILL-IN block and the
un-replaced placeholder thumbnail -- are unchanged.
