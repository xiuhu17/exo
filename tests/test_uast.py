from __future__ import annotations
import sys
sys.path.append(sys.path[0]+"/..")

from SYS_ATL import proc, Procedure

def test_conv1d():
  @proc
  def conv1d(n : size, m : size, r: size,
             x : R[n] @ IN, w : R[m] @ IN, res : R[r] @ OUT ):
    for i in range(0,r):
      res[i] = 0.0
    for i in range(0,r):
      for j in range(0,n):
        if i <= j < i + m:
          res[i] += x[j]*w[i-j+m-1]

  assert type(conv1d) is Procedure
  print(conv1d._TESTING_UAST())

# conv1d.reorder(j,i)
# ->
#    for i in range(0,r):
#      for j in range(0,n):
#        if i <= j < i + m:
#          res[i] += x[j]*w[i-j+m-1]
# 
# conv1d.split(j,2)
# ->
#    for i in range(0,r):
#      res[i] = 0.0
#    for i in range(0,r):
#      TODO: What is n/2?
#      for j_1 in range(0,n/2):
#        for j_2 in range(0,2):
#          j = j_1*2 + j_2
#          if i <= j < i + m:
#            res[i] += x[j]*w[i-j+m-1]
# OR ->
#    for i in range(0,r):
#      res[i] = 0.0
#    for i in range(0,r):
#      for j_1 in range(0,n):
#        for j_2 in range(0,2):
#          j = j_1*2 + j_2
#          if j < n:
#            if i <= j < i + m:
#              res[i] += x[j]*w[i-j+m-1]
#
# conv1d.fuse(i)
# ->
#    for i in range(0,r):
#      res[i] = 0.0
#      for j in range(0,n):
#        if i <= j < i + m:
#          res[i] += x[j]*w[i-j+m-1]
#
# conv1d.unroll(i,3)
# ->
# TODO: What's r/3?
#    for i in range(0,r/3):
#      res[i*3+1] = 0.0
#      res[i*3+2] = 0.0
#      res[i*3+3] = 0.0
#    for i in range(0,r/3):
#      for j in range(0,n):
#        if i*3+1 <= j < i*3+1 + m:
#          res[i*3+1] += x[j]*w[i*3+1-j+m-1]
#      for j in range(0,n):
#        if i*3+2 <= j < i*3+2 + m:
#          res[i*3+2] += x[j]*w[i*3+2-j+m-1]
#      for j in range(0,n):
#        if i*3+3 <= j < i*3+3 + m:
#          res[i*3+3] += x[j]*w[i*3+3-j+m-1]
#
# TODO: .parallel() and .vectorize() are not rewrite operations?
#       Need to introduce notation which is not a Loop IR