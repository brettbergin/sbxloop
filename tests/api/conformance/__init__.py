"""The remote API conformance suite (#1041).

Every scenario here is what a remote client can do with the public
contract alone — routes, bodies, headers, cursors and frames — against a
real daemon loop in process, and what it sees when things go wrong: a
second client with other grants, a duplicate admission, a cancel that
arrives late, a hold someone else took, a crash between accepting and
acting, a stream that lost its place, a token revoked mid-work. Each file
is one part of the loop: admit, watch, intervene, hold and resume, decide,
finish, retrieve.
"""
