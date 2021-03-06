import sys
from collections import defaultdict
import numpy as np
import os

import h5py
import fast64counter

import time
import copy

job_repeat_attempts = 5

def check_file(filename):
    if not os.path.exists(filename):
        return False
    # verify the file has the expected data
    import h5py
    f = h5py.File(filename, 'r')
    fkeys = f.keys()
    f.close()
    if set(fkeys) != set(['labels']) and set(fkeys) != set(['labels', 'merges']):
        os.unlink(filename)
        return False
    return True

Debug = False

single_image_matching = True

block1_path, block2_path, direction, halo_size, outblock1_path, outblock2_path = sys.argv[1:]
direction = int(direction)
halo_size = int(halo_size)

###############################
# Note: direction indicates the relative position of the blocks (1, 2, 3 =>
# adjacent in X, Y, Z).  Block1 is always closer to the 0,0,0 corner of the
# volume.
###############################

repeat_attempt_i = 0
while repeat_attempt_i < job_repeat_attempts and not (
    check_file(outblock1_path) and check_file(outblock2_path)):

    repeat_attempt_i += 1

    try:

        print 'Running pairwise matching', " ".join(sys.argv[1:])

        # Extract overlapping regions
        for ntry in range(5):
            try:
                bl1f = h5py.File(block1_path, 'r')
                block1 = bl1f['labels'][...]
                label_chunks = bl1f['labels'].chunks
                if 'merges' in bl1f:
                    previous_merges1 = bl1f['merges'][...]
                else:
                    previous_merges1 = None
                bl1f.close()

                bl2f = h5py.File(block2_path, 'r')
                block2 = bl2f['labels'][...]
                if 'merges' in bl2f:
                    previous_merges2 = bl2f['merges'][...]
                else:
                    previous_merges2 = None
                bl2f.close()

            except IOError:
                print "IOError reading hdf5 (try {0}). Waiting...".format(ntry)
                time.sleep(10)
                pass

        assert block1.size == block2.size


        # append the blocks, and pack them so we can use the fast 64-bit counter
        stacked = np.vstack((block1, block2))
        inverse, packed = np.unique(stacked, return_inverse=True)
        packed = packed.reshape(stacked.shape)
        packed_block1 = packed[:block1.shape[0], :, :]
        packed_block2 = packed[block1.shape[0]:, :, :]

        # extract overlap

        lo_block1 = [0, 0, 0];
        hi_block1 = [None, None, None]
        lo_block2 = [0, 0, 0];
        hi_block2 = [None, None, None]

        # Adjust for Matlab HDF5 storage order
        #direction = 3 - direction
        direction = direction - 1

        # Adjust overlapping region boundaries for direction
        lo_block1[direction] = - 2 * halo_size
        hi_block2[direction] = 2 * halo_size

        if single_image_matching:
            lo_block1[direction] = lo_block1[direction] + halo_size
            lo_block2[direction] = lo_block2[direction] + halo_size
            hi_block1[direction] = lo_block1[direction] + 1
            hi_block2[direction] = lo_block2[direction] + 1

        block1_slice = tuple(slice(l, h) for l, h in zip(lo_block1, hi_block1))
        block2_slice = tuple(slice(l, h) for l, h in zip(lo_block2, hi_block2))
        packed_overlap1 = packed_block1[block1_slice]
        packed_overlap2 = packed_block2[block2_slice]
        print "block1", block1_slice, packed_overlap1.shape
        print "block2", block2_slice, packed_overlap2.shape

        counter = fast64counter.ValueCountInt64()
        counter.add_values_pair32(packed_overlap1.astype(np.int32).ravel(), packed_overlap2.astype(np.int32).ravel())
        overlap_labels1, overlap_labels2, overlap_areas = counter.get_counts_pair32()

        areacounter = fast64counter.ValueCountInt64()
        areacounter.add_values(packed_overlap1.ravel())
        areacounter.add_values(packed_overlap2.ravel())
        areas = dict(zip(*areacounter.get_counts()))

        if Debug:
            from libtiff import TIFF

            # output full block images
            for image_i in range(block1.shape[2]):
                tif = TIFF.open('block1_z{0:04}.tif'.format(image_i), mode='w')
                tif.write_image(np.uint8(block1[:, :, image_i] * 13 % 251))
                tif = TIFF.open('block2_z{0:04}.tif'.format(image_i), mode='w')
                tif.write_image(np.uint8(block2[:, :, image_i] * 13 % 251))

            #output overlap images
            if single_image_matching:
                tif = TIFF.open('packed_overlap1.tif', mode='w')
                tif.write_image(np.uint8(np.squeeze(packed_overlap1) * 13 % 251))
                tif = TIFF.open('packed_overlap2.tif', mode='w')
                tif.write_image(np.uint8(np.squeeze(packed_overlap2) * 13 % 251))
            else:
                for image_i in range(packed_overlap1.shape[2]):
                    tif = TIFF.open('packed_overlap1_z{0:04}.tif'.format(image_i), mode='w')
                    tif.write_image(np.uint8(packed_overlap1[:, :, image_i] * 13 % 251))
                    tif = TIFF.open('packed_overlap2_z{0:04}.tif'.format(image_i), mode='w')
                    tif.write_image(np.uint8(packed_overlap2[:, :, image_i] * 13 % 251))

            # import pylab
            # pylab.figure()
            # pylab.imshow(block1[0, :, :] % 13)
            # pylab.title('block1')
            # pylab.figure()
            # pylab.imshow(block2[0, :, :] % 13)
            # pylab.title('block2')
            # pylab.figure()
            # pylab.imshow(packed_overlap1[0, :, :] % 13)
            # pylab.title('packed overlap1')
            # pylab.figure()
            # pylab.imshow(packed_overlap2[0, :, :] % 13)
            # pylab.title('packed overlap2')

            # pylab.show()

        # Merge with stable marrige matches best match = greatest overlap
        to_merge = []

        m_preference = {}
        w_preference = {}

        # Generate preference lists
        for l1, l2, overlap_area in zip(overlap_labels1, overlap_labels2, overlap_areas):
            if inverse[l1] != 0 and inverse[l2] != 0:
                if l1 not in m_preference:
                    m_preference[l1] = [(l2, overlap_area)]
                else:
                    m_preference[l1].append((l2, overlap_area))
                if l2 not in w_preference:
                    w_preference[l2] = [(l1, overlap_area)]
                else:
                    w_preference[l2].append((l1, overlap_area))
                print '{1} = {0} ({2} overlap).'.format(l1, l2, overlap_area)

        # Sort preference lists
        for mk in m_preference.keys():
            m_preference[mk] = sorted(m_preference[mk], key=lambda x:x[1], reverse=True)

        for wk in w_preference.keys():
            w_preference[wk] = sorted(w_preference[wk], key=lambda x:x[1], reverse=True)

        # Prep for proposals
        mlist = sorted(m_preference.keys())
        wlist = sorted(w_preference.keys())

        mfree = mlist[:]
        engaged  = {}
        mprefers2 = copy.deepcopy(m_preference)
        wprefers2 = copy.deepcopy(w_preference)

        # Stable marriage loop
        while mfree:
            m = mfree.pop(0)
            mlist = mprefers2[m]
            w = mlist.pop(0)[0]
            fiance = engaged.get(w)
            if not fiance:
                # She's free
                engaged[w] = m
                print("  {0} and {1} engaged".format(w, m))
            else:
                # m proposes w
                wlist = list(x[0] for x in wprefers2[w])
                if wlist.index(fiance) > wlist.index(m):
                    # w prefers new m
                    engaged[w] = m
                    print("  {0} dumped {1} for {2}".format(w, fiance, m))
                    if mprefers2[fiance]:
                        # m has more w to try
                        mfree.append(fiance)
                else:
                    # She is faithful to old fiance
                    if mlist:
                        # Look again
                        mfree.append(m)

        for l2 in engaged.keys():
            l1 = engaged[l2]
            print "Merging segments {1} and {0}.".format(l1, l2)
            to_merge.append((inverse[l1], inverse[l2]))

        # handle merges by rewriting the inverse
        merge_map = dict(reversed(sorted(s)) for s in to_merge)
        for idx, val in enumerate(inverse):
            if val in merge_map:
                while val in merge_map:
                    val = merge_map[val]
                inverse[idx] = val

        # Remap and merge
        out1 = h5py.File(outblock1_path + '_partial', 'w')
        out2 = h5py.File(outblock2_path + '_partial', 'w')
        outblock1 = out1.create_dataset('/labels', block1.shape, block1.dtype, chunks=label_chunks, compression='gzip')
        outblock2 = out2.create_dataset('/labels', block2.shape, block2.dtype, chunks=label_chunks, compression='gzip')
        outblock1[...] = inverse[packed_block1]
        outblock2[...] = inverse[packed_block2]

        # copy any previous merge tables from block 1 to the new output and merge
        if previous_merges1 != None:
            if len(to_merge):
                merges1 = np.vstack((previous_merges1, to_merge))
            else:
                merges1 = previous_merges1
        else:
            merges1 = np.array(to_merge).astype(np.uint64)

        if merges1.size > 0:
            out1.create_dataset('/merges', merges1.shape, merges1.dtype)[...] = merges1

        # copy any previous merge tables from block 2 to the new output
        if previous_merges2 != None:
            out2.create_dataset('/merges', previous_merges2.shape, previous_merges2.dtype)[...] = previous_merges2


        if Debug:

            # output full block images
            for image_i in range(block1.shape[2]):
                tif = TIFF.open('block1_final_z{0:04}.tif'.format(image_i), mode='w')
                tif.write_image(np.uint8(outblock1[:, :, image_i] * 13 % 251))
                tif = TIFF.open('block2_final_z{0:04}.tif'.format(image_i), mode='w')
                tif.write_image(np.uint8(outblock2[:, :, image_i] * 13 % 251))

            # import pylab
            # pylab.figure()
            # pylab.imshow(outblock1[0, :, :] % 13)
            # pylab.title('final block1')
            # pylab.figure()
            # pylab.imshow(outblock2[0, :, :] % 13)
            # pylab.title('final block2')
            # pylab.show()

        # move to final location
        out1.close()
        out2.close()

        if os.path.exists(outblock1_path):
                os.unlink(outblock1_path)
        if os.path.exists(outblock2_path):
                os.unlink(outblock2_path)

        os.rename(outblock1_path + '_partial', outblock1_path)
        os.rename(outblock2_path + '_partial', outblock2_path)
        print "Successfully wrote", outblock1_path, 'and', outblock2_path

    except IOError as e:
        print "I/O error({0}): {1}".format(e.errno, e.strerror)
    except KeyboardInterrupt:
        pass
    except:
        print "Unexpected error:", sys.exc_info()[0]
        if repeat_attempt_i == job_repeat_attempts:
            pass
        
assert (check_file(outblock1_path) and check_file(outblock2_path)), "Output files could not be verified after {0} attempts, exiting.".format(job_repeat_attempts)
